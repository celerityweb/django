"""
A Python "serializer". Doesn't do much serializing per se -- just converts to
and from basic Python data types (lists, dicts, strings, etc.). Useful as a basis for
other serializers.
"""
from __future__ import unicode_literals

from django.conf import settings
from django.contrib.contenttypes.generic import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.serializers import base
from django.db import models, DEFAULT_DB_ALIAS
from django.utils.encoding import smart_text, is_protected_type
from django.utils import six


class Serializer(base.Serializer):
    """
    Serializes a QuerySet to basic Python objects.
    """

    internal_use_only = True

    def start_serialization(self):
        self._current = None
        self.objects = []

    def end_serialization(self):
        pass

    def start_object(self, obj):
        self._current = {}

    def end_object(self, obj):
        self.naturalize_generics(obj)
        self.objects.append(self.get_dump_object(obj))
        self._current = None

    def get_dump_object(self, obj):
        return {
            "pk": smart_text(obj._get_pk_val(), strings_only=True),
            "model": smart_text(obj._meta),
            "fields": self._current
        }

    def handle_field(self, obj, field):
        value = field._get_val_from_obj(obj)
        # Protected types (i.e., primitives like None, numbers, dates,
        # and Decimals) are passed through as is. All other values are
        # converted to string first.
        if is_protected_type(value):
            self._current[field.name] = value
        else:
            self._current[field.name] = field.value_to_string(obj)

    def handle_fk_field(self, obj, field):
        if self.use_natural_keys and hasattr(field.rel.to, 'natural_key'):
            related = getattr(obj, field.name)
            if related:
                value = related.natural_key()
            else:
                value = None
        else:
            value = getattr(obj, field.get_attname())
        self._current[field.name] = value

    def handle_m2m_field(self, obj, field):
        if field.rel.through._meta.auto_created:
            if self.use_natural_keys and hasattr(field.rel.to, 'natural_key'):
                m2m_value = lambda value: value.natural_key()
            else:
                m2m_value = lambda value: smart_text(value._get_pk_val(), strings_only=True)
            self._current[field.name] = [m2m_value(related)
                               for related in getattr(obj, field.name).iterator()]

    def getvalue(self):
        return self.objects

    def naturalize_generics(self, obj):
        gfk_fields = [field for field in obj._meta.virtual_fields
                      if isinstance(field, GenericForeignKey)]
        if self.use_natural_keys:
            for gfk_field in gfk_fields:
                rel_obj = getattr(obj, gfk_field.name, None)
                if rel_obj:
                    ct_obj = getattr(obj, gfk_field.ct_field)
                    if hasattr(ct_obj.model_class(), 'natural_key'):
                        self._current[gfk_field.fk_field] = rel_obj.natural_key()




def Deserializer(object_list, **options):
    """
    Deserialize simple Python objects back into Django ORM instances.

    It's expected that you pass the Python objects themselves (instead of a
    stream or a string) to the constructor
    """
    db = options.pop('using', DEFAULT_DB_ALIAS)
    ignore = options.pop('ignorenonexistent', False)

    models.get_apps()
    for d in object_list:
        # Look up the model and starting build a dict of data for it.
        Model = _get_model(d["model"])
        data = {Model._meta.pk.attname: Model._meta.pk.to_python(d.get("pk", None))}
        m2m_data = {}
        model_fields = Model._meta.get_all_field_names()
        gfk_fk_fields = {field.fk_field: field.ct_field
                         for field in Model._meta.virtual_fields
                         if isinstance(field, GenericForeignKey)}
        deferred = {}

        # Handle each field
        for (field_name, field_value) in six.iteritems(d["fields"]):

            if ignore and field_name not in model_fields:
                # skip fields no longer on model
                continue

            if isinstance(field_value, str):
                field_value = smart_text(field_value, options.get("encoding", settings.DEFAULT_CHARSET), strings_only=True)

            field = Model._meta.get_field(field_name)

            # Handle M2M relations
            if field.rel and isinstance(field.rel, models.ManyToManyRel):
                if hasattr(field.rel.to._default_manager, 'get_by_natural_key'):
                    def m2m_convert(value):
                        if hasattr(value, '__iter__') and not isinstance(value, six.text_type):
                            return field.rel.to._default_manager.db_manager(db).get_by_natural_key(*value).pk
                        else:
                            return smart_text(field.rel.to._meta.pk.to_python(value))
                else:
                    m2m_convert = lambda v: smart_text(field.rel.to._meta.pk.to_python(v))
                m2m_data[field.name] = [m2m_convert(pk) for pk in field_value]

            # Handle FK fields
            elif field.rel and isinstance(field.rel, models.ManyToOneRel):
                if field_value is not None:
                    if hasattr(field.rel.to._default_manager, 'get_by_natural_key'):
                        if hasattr(field_value, '__iter__') and not isinstance(field_value, six.text_type):
                            obj = field.rel.to._default_manager.db_manager(db).get_by_natural_key(*field_value)
                            value = getattr(obj, field.rel.field_name)
                            # If this is a natural foreign key to an object that
                            # has a FK/O2O as the foreign key, use the FK value
                            if field.rel.to._meta.pk.rel:
                                value = value.pk
                        else:
                            value = field.rel.to._meta.get_field(field.rel.field_name).to_python(field_value)
                        data[field.attname] = value
                    else:
                        data[field.attname] = field.rel.to._meta.get_field(field.rel.field_name).to_python(field_value)
                else:
                    data[field.attname] = None

            # Defer GenericForeignKey to ensure ContentTypes were deserialized
            elif (field_name in gfk_fk_fields and hasattr(field_value, '__iter__') and
                      not isinstance(field_value, six.text_type)):
                data[field_name] = field_value
                deferred[field_name] = gfk_fk_fields[field_name]

            # Handle all other fields
            else:
                data[field.name] = field.to_python(field_value)

        for fk_field, ct_field in six.iteritems(deferred):
            ct_obj = ContentType.objects.get_for_id(data[ct_field+'_id'])
            rel_to = ct_obj.model_class()
            natural_key = data.get(fk_field)
            data[fk_field] = rel_to._default_manager.get_by_natural_key(*natural_key).pk

        yield base.DeserializedObject(Model(**data), m2m_data)

def _get_model(model_identifier):
    """
    Helper to look up a model from an "app_label.model_name" string.
    """
    try:
        Model = models.get_model(*model_identifier.split("."))
    except TypeError:
        Model = None
    if Model is None:
        raise base.DeserializationError("Invalid model identifier: '%s'" % model_identifier)
    return Model
