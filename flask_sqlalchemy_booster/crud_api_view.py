from flask.views import MethodView
from flask import g, request, Response
from schemalite import SchemaError
from schemalite.core import validate_object, validate_list_of_objects, json_encoder
from sqlalchemy.sql import sqltypes
import json
from toolspy import all_subclasses
from copy import deepcopy

from .responses import (
    process_args_and_render_json_list, success_json, error_json,
    render_json_obj_with_requested_structure,
    render_json_list_with_requested_structure,
    _serializable_params, serializable_obj, as_json)


def construct_get_view_function(
        model_class, registration_dict,
        permitted_object_getter=None,
        dict_struct=None, schemas_registry=None, get_query_creator=None):
    def get(_id):
        _id = _id.strip()
        if _id.startswith('[') and _id.endswith(']'):
            if permitted_object_getter is not None:
                resources = [permitted_object_getter()]
                ids = [_id[1:-1]]
            else:
                ids = [int(i) for i in json.loads(_id)]
                if get_query_creator:
                    resources = get_query_creator(model_class.query).get_all(ids)
                else:
                    resources = model_class.get_all(ids)
            if None in resources:
                if all(r is None for r in resources):
                    status = "failure"
                else:
                    status = "partial_success"
            else:
                status = "success"
            return render_json_list_with_requested_structure(
                resources,
                pre_render_callback=lambda output_dict: {
                    'status': status,
                    'result': {
                        _id: {'status': 'failure', 'error': 'Resource not found'}
                        if obj is None
                        else {'status': 'success', 'result': obj}
                        for _id, obj in zip(ids, output_dict['result'])}
                },
                dict_struct=dict_struct
            )
        if permitted_object_getter is not None:
            obj = permitted_object_getter()
        else:
            if get_query_creator:
                obj = get_query_creator(model_class.query).get(_id)
            else:
                obj = model_class.get(_id)
        if obj is None:
            return error_json(404, 'Resource not found')
        return render_json_obj_with_requested_structure(obj, dict_struct=dict_struct)
    return get


def construct_index_view_function(
        model_class, index_query_creator=None, dict_struct=None):
    def index():
        if callable(index_query_creator):
            return process_args_and_render_json_list(
                index_query_creator(model_class.query),
                dict_struct=dict_struct)
        return process_args_and_render_json_list(model_class, dict_struct=dict_struct)

    return index


def construct_post_view_function(
        model_class, schema, registration_dict, pre_processors=None,
        post_processors=None,
        allow_unknown_fields=False,
        dict_struct=None, schemas_registry=None):

    def post():
        if pre_processors is not None:
            for processor in pre_processors:
                if callable(processor):
                    processor()
        if isinstance(g.json, list):
            input_data = model_class.pre_validation_adapter_for_list(g.json)
            if isinstance(input_data, Response):
                return input_data
            is_valid, errors = validate_list_of_objects(
                schema, input_data, context={"model_class": model_class},
                allow_unknown_fields=allow_unknown_fields,
                schemas_registry=schemas_registry)
            input_objs = input_data
            if not is_valid:
                input_objs = [
                    input_obj if error is None else None
                    for input_obj, error in zip(input_data, errors)]
            resources = model_class.create_all(input_objs)
            if post_processors is not None:
                for processor in post_processors:
                    if callable(processor):
                        for resource, datum in zip(resources, input_data):
                            processor(resource, datum)
            if None in resources:
                if all(r is None for r in resources):
                    status = "failure"
                else:
                    status = "partial_success"
            else:
                status = "success"
            return render_json_list_with_requested_structure(
                resources,
                pre_render_callback=lambda output_dict: {
                    'status': status,
                    'result': [
                        {'status': 'failure', 'error': error}
                        if obj is None
                        else
                        {'status': 'success', 'result': obj}
                        for obj, error in zip(output_dict['result'], errors)]})
        else:
            input_data = model_class.pre_validation_adapter(g.json)
            if isinstance(input_data, Response):
                return input_data
            is_valid, errors = validate_object(
                schema, input_data, context={"model_class": model_class},
                schemas_registry=schemas_registry,
                allow_unknown_fields=allow_unknown_fields)
            if not is_valid:
                return error_json(400, errors)
            obj = model_class.create(**input_data)
            if post_processors is not None:
                for processor in post_processors:
                    if callable(processor):
                        processor(obj, input_data)
            if '_ret' in g.args:
                rels = g.args['_ret'].split(".")
                final_obj = obj
                for rel in rels:
                    final_obj = getattr(final_obj, rel)
                final_obj_cls = type(final_obj)
                final_obj_dict_struct = None
                if final_obj_cls in registration_dict:
                    final_obj_dict_struct = registration_dict[final_obj_cls].get('dict_struct')
                return render_json_obj_with_requested_structure(final_obj, dict_struct=final_obj_dict_struct)
            return render_json_obj_with_requested_structure(obj, dict_struct=dict_struct)
    return post


def construct_put_view_function(
        model_class, schema, pre_processors=None,
        post_processors=None,
        query_constructor=None, schemas_registry=None,
        permitted_object_getter=None,
        dict_struct=None,
        allow_unknown_fields=False):
    def put(_id):
        if permitted_object_getter is not None:
            obj = permitted_object_getter()
        else:
            if callable(query_constructor):
                obj = query_constructor(model_class.query).get(_id)
            else:
                obj = model_class.get(_id)
        if obj is None:
            return error_json(404, 'Resource not found')
        if pre_processors is not None:
            for processor in pre_processors:
                if callable(processor):
                    processor(obj)
        input_data = model_class.pre_validation_adapter(g.json, existing_instance=obj)
        if isinstance(input_data, Response):
            return input_data
        polymorphic_field = schema.get('polymorphic_on')
        if polymorphic_field:
            if polymorphic_field not in input_data:
                input_data[polymorphic_field] = getattr(obj, polymorphic_field)
        is_valid, errors = validate_object(
            schema, input_data, allow_required_fields_to_be_skipped=True,
            allow_unknown_fields=allow_unknown_fields,
            context={"existing_instance": obj,
                     "model_class": model_class},
            schemas_registry=schemas_registry)
        if not is_valid:
            return error_json(400, errors)
        updated_obj = obj.update(**input_data)
        if post_processors is not None:
            for processor in post_processors:
                if callable(processor):
                    processor(updated_obj, input_data)
        return render_json_obj_with_requested_structure(
            updated_obj,
            dict_struct=dict_struct)

    return put


def construct_batch_put_view_function(
        model_class, schema, pre_processors=None,
        post_processors=None,
        query_constructor=None, schemas_registry=None,
        allow_unknown_fields=False):

    def batch_put():
        if pre_processors is not None:
            for processor in pre_processors:
                if callable(processor):
                    processor()
        output = {}
        obj_ids = g.json.keys()
        if type(model_class.primary_key().type)==sqltypes.Integer:
            obj_ids = [int(obj_id) for obj_id in obj_ids]
        if callable(query_constructor):
            objs = query_constructor(model_class.query).get_all(obj_ids)
        else:
            objs = model_class.get_all(obj_ids)
        existing_instances = dict(zip(obj_ids, objs))
        all_success = True
        any_success = False
        polymorphic_field = schema.get('polymorphic_on')
        input_data = model_class.pre_validation_adapter_for_mapped_collection(g.json, existing_instances)
        if isinstance(input_data, Response):
            return input_data
        updated_objects = {}
        for obj_id, put_data_for_obj in input_data.items():
            output_key = obj_id
            if type(model_class.primary_key().type)==sqltypes.Integer:
                output_key = int(obj_id)
            existing_instance = existing_instances[output_key]
            if existing_instance is None:
                output[output_key] = {
                    "status": "failure",
                    "result": "Resource not found"
                }
                all_success = False
                any_success = any_success or False
            else:
                if polymorphic_field:
                    if polymorphic_field not in put_data_for_obj:
                        put_data_for_obj[polymorphic_field] = getattr(existing_instance, polymorphic_field)
                is_valid, errors = validate_object(
                    schema, put_data_for_obj, allow_required_fields_to_be_skipped=True,
                    allow_unknown_fields=allow_unknown_fields,
                    context={
                        "existing_instance": existing_instance,
                        "model_class": model_class
                    }, schemas_registry=schemas_registry)
                if is_valid:
                    updated_object = existing_instance.update_without_commit(
                        **put_data_for_obj)
                    updated_objects[updated_object.id] = updated_object
                    output[output_key] = {
                        "status": "success",
                        "result": serializable_obj(
                            updated_object,
                            **_serializable_params(request.args))
                    }
                    all_success = all_success and True
                    any_success = True
                else:
                    output[output_key] = {
                        "status": "failure",
                        "error": errors
                    }
                    all_success = False
                    any_success = any_success or False
        if post_processors is not None:
            for processor in post_processors:
                if callable(processor):
                    processor(updated_objects, input_data)
        final_status = "success"
        if not all_success:
            if any_success:
                final_status = "partial_success"
            else:
                final_status = "failure"
        return as_json({
            "status": final_status,
            "result": output
        }, wrap=False)
    return batch_put


def construct_patch_view_function(model_class, schema, pre_processors=None,
                                  query_constructor=None, schemas_registry=None):
    def patch(_id):
        if pre_processors is not None:
            for processor in pre_processors:
                if callable(processor):
                    processor()
        if callable(query_constructor):
            obj = query_constructor(model_class.query).get(_id)
        else:
            obj = model_class.get(_id)
        polymorphic_field = schema.get('polymorphic_on')
        if polymorphic_field:
            if polymorphic_field not in g.json:
                g.json[polymorphic_field] = getattr(obj, polymorphic_field)
        is_valid, errors = validate_object(
            schema, g.json, allow_required_fields_to_be_skipped=True,
            context={"existing_instance": obj,
                     "model_class": model_class},
            schemas_registry=schemas_registry)
        if not is_valid:
            return error_json(400, errors)
        return render_json_obj_with_requested_structure(obj.update(**g.json))

    return patch


def construct_delete_view_function(model_class, query_constructor=None):
    def delete(_id):
        if callable(query_constructor):
            obj = query_constructor(model_class.query).get(_id)
        else:
            obj = model_class.get(_id)
        if obj is None:
            return error_json(404, 'Resource not found')
        obj.delete()
        return success_json()
    return delete


def register_crud_routes_for_models(
        app_or_bp, registration_dict, register_schema_structure=True,
        allow_unknown_fields=False):
    if not hasattr(app_or_bp, "registered_models_and_crud_routes"):
        app_or_bp.registered_models_and_crud_routes = {
            "models_registered_for_views": [],
            "model_schemas": {

            },
            "views": {

            }
        }
    model_schemas = app_or_bp.registered_models_and_crud_routes["model_schemas"]

    def populate_model_schema(modelcls):
        if modelcls._input_data_schema_:
            input_schema = deepcopy(modelcls._input_data_schema_)
        else:
            input_schema = modelcls.generate_input_data_schema()
        if modelcls in registration_dict and callable(registration_dict[modelcls].get('input_schema_modifier')):
            input_schema = registration_dict[modelcls]['input_schema_modifier'](input_schema)
        model_schemas[modelcls.__name__] = {
            "input_schema": input_schema,
            "output_schema": modelcls.output_data_schema(),
            "accepted_data_structure": modelcls.max_permissible_dict_structure()
        }
        for subcls in all_subclasses(modelcls):
            if subcls.__name__ not in model_schemas:
                model_schemas[subcls.__name__] = {
                    'is_a_polymorphically_derived_from': modelcls.__name__,
                    'polymorphic_identity': subcls.__mapper_args__['polymorphic_identity'] 
                }
        for rel in modelcls.__mapper__.relationships.values():
            if rel.mapper.class_.__name__ not in model_schemas:
                populate_model_schema(rel.mapper.class_)

    for _model, _model_dict in registration_dict.items():
        base_url = _model_dict.get('url_slug')
        forbidden_views = _model_dict.get('forbidden_views', [])
        default_query_constructor = _model_dict.get('query_constructor')
        view_dict_for_model = _model_dict.get('views', {})
        dict_struct_for_model = _model_dict.get('dict_struct')
        resource_name = _model.__tablename__

        if _model.__name__ not in app_or_bp.registered_models_and_crud_routes["models_registered_for_views"]:
            app_or_bp.registered_models_and_crud_routes["models_registered_for_views"].append(_model.__name__)
        if _model.__name__ not in model_schemas:
            populate_model_schema(_model)

        if _model._input_data_schema_:
            model_default_input_schema = deepcopy(_model._input_data_schema_)
        else:
            model_default_input_schema = _model.generate_input_data_schema()
        if callable(registration_dict[_model].get('input_schema_modifier')):
            model_default_input_schema = registration_dict[_model]['input_schema_modifier'](model_default_input_schema)

        views = app_or_bp.registered_models_and_crud_routes["views"]
        schemas_registry = {k: v.get('input_schema') for k, v in model_schemas.items()}
        if _model.__name__ not in views:
            views[_model.__name__] = {}

        if 'index' not in forbidden_views:
            index_dict = view_dict_for_model.get('index', {})
            index_func = index_dict.get('view_func', None) or construct_index_view_function(
                _model,
                index_query_creator=index_dict.get('query_constructor') or default_query_constructor,
                dict_struct=index_dict.get('dict_struct') or dict_struct_for_model)
            index_url = index_dict.get('url', None) or "/%s" % base_url
            app_or_bp.route(
                index_url, methods=['GET'], endpoint='index_%s' % resource_name)(
                index_func)
            views[_model.__name__]['index'] = {'url': index_url}

        if 'get' not in forbidden_views:
            get_dict = view_dict_for_model.get('get', {})
            get_func = get_dict.get('view_func', None) or construct_get_view_function(
                _model, registration_dict,
                permitted_object_getter=get_dict.get('permitted_object_getter') or _model_dict.get('permitted_object_getter'),
                get_query_creator=get_dict.get('query_constructor') or default_query_constructor,
                dict_struct=get_dict.get('dict_struct') or dict_struct_for_model)
            get_url = get_dict.get('url', None) or '/%s/<_id>' % base_url
            app_or_bp.route(
                get_url, methods=['GET'], endpoint='get_%s' % resource_name)(
                get_func)
            views[_model.__name__]['get'] = {'url': get_url}

        if 'post' not in forbidden_views:
            post_dict = view_dict_for_model.get('post', {})
            if callable(post_dict.get('input_schema_modifier')):
                post_input_schema = post_dict['input_schema_modifier'](deepcopy(model_default_input_schema))
            else:
                post_input_schema = model_default_input_schema
            post_func = post_dict.get('view_func', None) or construct_post_view_function(
                _model, post_input_schema, registration_dict,
                post_dict.get('pre_processors'),
                post_processors=post_dict.get('post_processors'),
                schemas_registry=schemas_registry,
                allow_unknown_fields=allow_unknown_fields,
                dict_struct=post_dict.get('dict_struct') or dict_struct_for_model)
            post_url = post_dict.get('url', None) or "/%s" % base_url
            app_or_bp.route(
                post_url, methods=['POST'], endpoint='post_%s' % resource_name)(
                post_func)
            views[_model.__name__]['post'] = {'url': post_url}
            if 'input_schema_modifier' in post_dict:
                views[_model.__name__]['post']['input_schema'] = post_dict['input_schema_modifier'](
                    deepcopy(model_schemas[_model.__name__]['input_schema']))

        if 'put' not in forbidden_views:
            put_dict = view_dict_for_model.get('put', {})
            if callable(put_dict.get('input_schema_modifier')):
                put_input_schema = put_dict['input_schema_modifier'](deepcopy(model_default_input_schema))
            else:
                put_input_schema = model_default_input_schema
            put_func = put_dict.get('view_func', None) or construct_put_view_function(
                _model, put_input_schema,
                permitted_object_getter=put_dict.get('permitted_object_getter') or _model_dict.get('permitted_object_getter'),
                pre_processors=put_dict.get('pre_processors'),
                post_processors=put_dict.get('post_processors'),
                dict_struct=put_dict.get('dict_struct') or dict_struct_for_model,
                allow_unknown_fields=allow_unknown_fields,
                query_constructor=put_dict.get('query_constructor') or default_query_constructor,
                schemas_registry=schemas_registry)
            put_url = put_dict.get('url', None) or "/%s/<_id>" % base_url
            app_or_bp.route(
                put_url, methods=['PUT'], endpoint='put_%s' % resource_name)(
                put_func)
            views[_model.__name__]['put'] = {'url': put_url}
            if 'input_schema_modifier' in put_dict:
                views[_model.__name__]['put']['input_schema'] = put_dict['input_schema_modifier'](
                    deepcopy(model_schemas[_model.__name__]['input_schema']))

        if 'batch_put' not in forbidden_views:
            batch_put_dict = view_dict_for_model.get('batch_put', {})
            if callable(batch_put_dict.get('input_schema_modifier')):
                batch_put_input_schema = batch_put_dict['input_schema_modifier'](deepcopy(model_default_input_schema))
            else:
                batch_put_input_schema = model_default_input_schema
            batch_put_func = batch_put_dict.get('view_func', None) or construct_batch_put_view_function(
                _model, batch_put_input_schema,
                batch_put_dict.get('pre_processors'),
                post_processors=batch_put_dict.get('post_processors'),
                allow_unknown_fields=allow_unknown_fields,
                query_constructor=batch_put_dict.get('query_constructor') or default_query_constructor,
                schemas_registry=schemas_registry)
            batch_put_url = batch_put_dict.get('url', None) or "/%s" % base_url
            app_or_bp.route(
                batch_put_url, methods=['PUT'], endpoint='batch_put_%s' % resource_name)(
                batch_put_func)
            views[_model.__name__]['batch_put'] = {'url': batch_put_url}
            if 'input_schema_modifier' in batch_put_dict:
                views[_model.__name__]['batch_put']['input_schema'] = batch_put_dict['input_schema_modifier'](
                    deepcopy(model_schemas[_model.__name__]['input_schema']))

        if 'patch' not in forbidden_views:
            patch_dict = view_dict_for_model.get('patch', {})
            if callable(patch_dict.get('input_schema_modifier')):
                patch_input_schema = patch_dict['input_schema_modifier'](deepcopy(model_default_input_schema))
            else:
                patch_input_schema = model_default_input_schema
            patch_func = patch_dict.get('view_func', None) or construct_patch_view_function(
                _model, patch_input_schema,
                patch_dict.get('pre_processors'),
                query_constructor=patch_dict.get('query_constructor') or default_query_constructor,
                schemas_registry=schemas_registry)
            patch_url = patch_dict.get('url', None) or "/%s/<_id>" % base_url
            app_or_bp.route(
                patch_url, methods=['PATCH'], endpoint='patch_%s' % resource_name)(
                patch_func)
            views[_model.__name__]['patch'] = {'url': patch_url}
            if 'input_schema_modifier' in patch_dict:
                views[_model.__name__]['patch']['input_schema'] = patch_dict['input_schema_modifier'](
                    deepcopy(model_schemas[_model.__name__]['input_schema']))

        if 'delete' not in forbidden_views:
            delete_dict = view_dict_for_model.get('delete', {})
            delete_func = delete_dict.get('view_func', None) or construct_delete_view_function(
                _model, query_constructor=delete_dict.get('query_constructor') or default_query_constructor)
            delete_url = delete_dict.get('url', None) or "/%s/<_id>" % base_url
            app_or_bp.route(
                delete_url, methods=['DELETE'], endpoint='delete_%s' % resource_name)(
                delete_func)
            views[_model.__name__]['delete'] = {'url': delete_url}



## TO BE DEPRECATED


class CrudApiView(MethodView):

    _model_class_ = None
    _list_query_ = None
    _id_key_ = 'id'
    _schema_for_post_ = None
    _schema_for_put_ = None

    def get(self, _id):
        list_query = self._list_query_ or self._model_class_.query
        if _id is None:
            return process_args_and_render_json_list(list_query)
        else:
            _id = _id.strip()
            if _id.startswith('[') and _id.endswith(']'):
                ids = [int(i) for i in json.loads(_id)]
                resources = self._model_class_.get_all(ids)
                if all(r is None for r in resources):
                    return error_json(404, "No matching resources found")
                return render_json_list_with_requested_structure(
                    resources,
                    pre_render_callback=lambda output_dict: {
                        'status': 'partial_success' if None in resources else 'success',
                        'result': [
                            {'status': 'failure', 'error': 'Resource not found'}
                            if obj is None
                            else
                            {'status': 'success', 'result': obj}
                            for obj in output_dict['result']]})
                return process_args_and_render_json_list(
                    self._model_class_.query.filter(
                        self._model_class_.primary_key().in_(ids)))
            return render_json_obj_with_requested_structure(
                self._model_class_.get(_id, key=self._id_key_))

    def post(self):
        if self._schema_for_post_:
            try:
                if isinstance(g.json, list):
                    self._schema_for_post_.validate_list(g.json)
                else:
                    self._schema_for_post_.validate(g.json)
            except SchemaError as e:
                return error_json(400, e.value)
            json_data = g.json
            # json_data = self._schema_for_post_.adapt(g.json)
        else:
            json_data = g.json
        if isinstance(g.json, list):
            return render_json_list_with_requested_structure(
                self._model_class_.create_all(json_data))
        return render_json_obj_with_requested_structure(
            self._model_class_.create(**json_data))

    def put(self, _id):
        obj = self._model_class_.get(_id, key=self._id_key_)
        if self._schema_for_put_:
            try:
                self._schema_for_put_.validate(g.json)
            except SchemaError as e:
                return error_json(400, e.value)
            json_data = self._schema_for_put_.adapt(g.json)
        else:
            json_data = g.json
        return render_json_obj_with_requested_structure(obj.update(**json_data))

    def patch(self, _id):
        obj = self._model_class_.get(_id, key=self._id_key_)
        json_data = g.json
        return render_json_obj_with_requested_structure(obj.update(**json_data))

    def delete(self, _id):
        obj = self._model_class_.get(_id, key=self._id_key_)
        obj.delete()
        return success_json()


def register_crud_api_view(view, bp_or_app, endpoint, url_slug):
    bp_or_app.add_url_rule(
        '/%s/' % url_slug, defaults={'_id': None},
        view_func=view.as_view('%s__INDEX' % endpoint), methods=['GET', ])
    bp_or_app.add_url_rule(
        '/%s' % url_slug, view_func=view.as_view('%s__POST' % endpoint), methods=['POST', ])
    bp_or_app.add_url_rule(
        '/%s/<_id>' % url_slug, view_func=view.as_view('%s__GET' % endpoint),
        methods=['GET'])
    bp_or_app.add_url_rule(
        '/%s/<_id>' % url_slug, view_func=view.as_view('%s__PUT' % endpoint),
        methods=['PUT'])
    bp_or_app.add_url_rule(
        '/%s/<_id>' % url_slug, view_func=view.as_view('%s__PATCH' % endpoint),
        methods=['PATCH'])
    bp_or_app.add_url_rule(
        '/%s/<_id>' % url_slug, view_func=view.as_view('%s__DELETE' % endpoint),
        methods=['DELETE'])