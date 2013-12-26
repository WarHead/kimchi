#
# Project Kimchi
#
# Copyright IBM, Corp. 2013
#
# Authors:
#  Adam Litke <agl@linux.vnet.ibm.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import cherrypy
import json
import urllib2


from functools import wraps
from jsonschema import Draft3Validator, ValidationError


import kimchi.template
from kimchi import auth
from kimchi.exception import InvalidOperation, InvalidParameter, MissingParameter
from kimchi.exception import NotFoundError,  OperationFailed
from kimchi.model import ISO_POOL_NAME


def get_class_name(cls):
    try:
        sub_class = cls.__subclasses__()[0]
    except AttributeError:
        sub_class = cls.__class__.__name__
    return sub_class.lower()


def model_fn(cls, fn_name):
    return '%s_%s' % (get_class_name(cls), fn_name)


def validate_method(allowed):
    method = cherrypy.request.method.upper()
    if method not in allowed:
        raise cherrypy.HTTPError(405)
    return method


def mime_in_header(header, mime):
    if not header in cherrypy.request.headers:
        accepts = 'application/json'
    else:
        accepts = cherrypy.request.headers[header]

    if accepts.find(';') != -1:
        accepts, _ = accepts.split(';', 1)

    if mime in accepts.split(','):
        return True

    return False


def parse_request():
    if 'Content-Length' not in cherrypy.request.headers:
        return {}
    rawbody = cherrypy.request.body.read()

    if mime_in_header('Content-Type', 'application/json'):
        try:
            return json.loads(rawbody)
        except ValueError:
            raise cherrypy.HTTPError(400, "Unable to parse JSON request")
    else:
        raise cherrypy.HTTPError(415, "This API only supports"
                                      " 'application/json'")
def internal_redirect(url):
    raise cherrypy.InternalRedirect(url.encode("utf-8"))


def validate_params(params, instance, action):
    root = cherrypy.request.app.root
    if hasattr(root, 'api_schema'):
        api_schema = root.api_schema
    else:
        return
    operation = model_fn(instance, action)
    validator = Draft3Validator(api_schema)
    request = {operation: params}
    try:
        validator.validate(request)
    except ValidationError:
        raise InvalidParameter('; '.join(
            e.message for e in validator.iter_errors(request)))


class Resource(object):
    """
    A Resource represents a single entity in the API (such as a Virtual Machine)

    To create new Resource types, subclass this and change the following things
    in the child class:

    - If the Resource requires more than one identifier set self.model_args as
      appropriate.  This should only be necessary if this Resource is logically
      nested.  For example: A Storage Volume belongs to a Storage Pool so the
      Storage Volume would set model args to (pool_ident, volume_ident).

    - Implement the base operations of 'lookup' and 'delete' in the model(s).

    - Set the 'data' property to a JSON-serializable representation of the
      Resource.
    """
    def __init__(self, model, ident=None):
        self.model = model
        self.ident = ident
        self.model_args = (ident,)
        self.update_params = []

    def generate_action_handler(self, action_name, action_args=None):
        def wrapper(*args, **kwargs):
            validate_method(('POST'))
            try:
                model_args = list(self.model_args)
                if action_args is not None:
                    model_args.extend(parse_request()[key] for key in action_args)
                fn = getattr(self.model, model_fn(self, action_name))
                fn(*model_args)
                raise internal_redirect(self.uri_fmt %
                                        tuple(self.model_args))
            except MissingParameter, param:
                raise cherrypy.HTTPError(400, "Missing parameter: '%s'" % param)
            except InvalidParameter, param:
                raise cherrypy.HTTPError(400, "Invalid parameter: '%s'" % param)
            except InvalidOperation, msg:
                raise cherrypy.HTTPError(400, "Invalid operation: '%s'" % msg)
            except OperationFailed, msg:
                raise cherrypy.HTTPError(500, "Operation Failed: '%s'" % msg)
            except NotFoundError, msg:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % msg)

        wrapper.__name__ = action_name
        wrapper.exposed = True
        return wrapper

    def lookup(self):
        try:
            lookup = getattr(self.model, model_fn(self, 'lookup'))
            self.info = lookup(*self.model_args)
        except AttributeError:
            self.info = {}

    def delete(self):
        try:
            fn = getattr(self.model, model_fn(self, 'delete'))
            fn(*self.model_args)
            cherrypy.response.status = 204
        except AttributeError:
            raise cherrypy.HTTPError(405, 'Delete is not allowed for %s' % get_class_name(self))
        except OperationFailed, msg:
            raise cherrypy.HTTPError(500, "Operation Failed: '%s'" % msg)
        except InvalidOperation, msg:
            raise cherrypy.HTTPError(400, "Invalid operation: '%s'" % msg)

    @cherrypy.expose
    def index(self):
        method = validate_method(('GET', 'DELETE', 'PUT'))
        if method == 'GET':
            try:
                return self.get()
            except NotFoundError, msg:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % msg)
            except InvalidOperation, msg:
                raise cherrypy.HTTPError(400, "Invalid operation: '%s'" % msg)
            except OperationFailed, msg:
                raise cherrypy.HTTPError(406, "Operation failed: '%s'" % msg)
        elif method == 'DELETE':
            try:
                return self.delete()
            except NotFoundError, msg:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % msg)
        elif method == 'PUT':
            try:
                return self.update()
            except InvalidParameter, msg:
                raise cherrypy.HTTPError(400, "Invalid parameter: '%s'" % msg)
            except InvalidOperation, msg:
                raise cherrypy.HTTPError(400, "Invalid operation: '%s'" % msg)
            except NotFoundError, msg:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % msg)

    def update(self):
        try:
            update = getattr(self.model, model_fn(self, 'update'))
        except AttributeError:
            raise cherrypy.HTTPError(405, "%s does not implement update "
                                     "method" % get_class_name(self))
        params = parse_request()
        validate_params(params, self, 'update')
        if self.update_params != None:
            invalids = [v for v in params.keys() if
                        v not in self.update_params]
            if invalids:
                raise cherrypy.HTTPError(405, "%s are not allowed to be updated" %
                                         invalids)
        ident = update(self.ident, params)
        if ident != self.ident:
            raise cherrypy.HTTPRedirect(self.uri_fmt %
                                        tuple(list(self.model_args[:-1]) + [urllib2.quote(ident.encode('utf8'))]),
                                        303)
        return self.get()


    def get(self):
        self.lookup()
        return kimchi.template.render(get_class_name(self), self.data)

    @property
    def data(self):
        """
        Override this in inherited classes to provide the Resource
        representation as a python dictionary.
        """
        return {}


class Collection(object):
    """
    A Collection is a container for Resource objects.  To create a new
    Collection type, subclass this and make the following changes to the child
    class:

    - Set self.resource to the type of Resource that this Collection contains

    - Set self.resource_args.  This can remain an empty list if the Resources
      can be initialized with only one identifier.  Otherwise, include
      additional values as needed (eg. to identify a parent resource).

    - Set self.model_args.  Similar to above, this is needed only if the model
      needs additional information to identify this Collection.

    - Implement the base operations of 'create' and 'get_list' in the model.
    """
    def __init__(self, model):
        self.model = model
        self.resource = Resource
        self.resource_args = []
        self.model_args = []

    def create(self, *args):
        try:
            create = getattr(self.model, model_fn(self, 'create'))
        except AttributeError:
            raise cherrypy.HTTPError(405,
                'Create is not allowed for %s' % get_class_name(self))
        params = parse_request()
        validate_params(params, self, 'create')
        args = self.model_args + [params]
        name = create(*args)
        cherrypy.response.status = 201
        args = self.resource_args + [name]
        res = self.resource(self.model, *args)
        return res.get()

    def _get_resources(self):
        try:
            get_list = getattr(self.model, model_fn(self, 'get_list'))
            idents = get_list(*self.model_args)
            res_list = []
            for ident in idents:
                # internal text, get_list changes ident to unicode for sorted
                args = self.resource_args + [ident]
                res = self.resource(self.model, *args)
                res.lookup()
                res_list.append(res)
            return res_list
        except AttributeError:
            return []

    def _cp_dispatch(self, vpath):
        if vpath:
            ident = vpath.pop(0)
            # incoming text, from URL, is not unicode, need decode
            args = self.resource_args + [ident.decode("utf-8")]
            return self.resource(self.model, *args)

    def get(self):
        resources = self._get_resources()
        data = []
        for res in resources:
            data.append(res.data)
        return kimchi.template.render(get_class_name(self), data)

    @cherrypy.expose
    def index(self, *args):
        method = validate_method(('GET', 'POST'))
        if method == 'GET':
            try:
                return self.get()
            except InvalidOperation, param:
                raise cherrypy.HTTPError(400,
                                         "Invalid operation: '%s'" % param)
            except NotFoundError, param:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % param)
        elif method == 'POST':
            try:
                return self.create(*args)
            except MissingParameter, param:
                raise cherrypy.HTTPError(400, "Missing parameter: '%s'" % param)
            except InvalidParameter, param:
                raise cherrypy.HTTPError(400, "Invalid parameter: '%s'" % param)
            except OperationFailed, param:
                raise cherrypy.HTTPError(500, "Operation Failed: '%s'" % param)
            except InvalidOperation, param:
                raise cherrypy.HTTPError(400,
                                         "Invalid operation: '%s'" % param)
            except NotFoundError, param:
                raise cherrypy.HTTPError(404, "Not found: '%s'" % param)


class AsyncCollection(Collection):
    """
    A Collection to create it's resource by asynchronous task
    """
    def __init__(self, model):
        super(AsyncCollection, self).__init__(model)

    def create(self, *args):
        try:
            create = getattr(self.model, model_fn(self, 'create'))
        except AttributeError:
            raise cherrypy.HTTPError(405,
                'Create is not allowed for %s' % get_class_name(self))
        params = parse_request()
        args = self.model_args + [params]
        task = create(*args)
        cherrypy.response.status = 202
        return kimchi.template.render("Task", task)


class DebugReportContent(Resource):
    def __init__(self, model, ident):
        super(DebugReportContent, self).__init__(model, ident)

    def get(self):
        self.lookup()
        raise internal_redirect(self.info['file'])


class VMs(Collection):
    def __init__(self, model):
        super(VMs, self).__init__(model)
        self.resource = VM


class VM(Resource):
    def __init__(self, model, ident):
        super(VM, self).__init__(model, ident)
        self.update_params = ["name"]
        self.screenshot = VMScreenShot(model, ident)
        self.uri_fmt = '/vms/%s'
        self.start = self.generate_action_handler('start')
        self.stop = self.generate_action_handler('stop')
        self.connect = self.generate_action_handler('connect')

    @property
    def data(self):
        return {'name': self.ident,
                'uuid': self.info['uuid'],
                'stats': self.info['stats'],
                'memory': self.info['memory'],
                'cpus': self.info['cpus'],
                'state': self.info['state'],
                'screenshot': self.info['screenshot'],
                'icon': self.info['icon'],
                'graphics': {'type': self.info['graphics']['type'],
                             'port': self.info['graphics']['port']}}


class VMScreenShot(Resource):
    def __init__(self, model, ident):
        super(VMScreenShot, self).__init__(model, ident)

    def get(self):
        self.lookup()
        raise internal_redirect(self.info)

class Templates(Collection):
    def __init__(self, model):
        super(Templates, self).__init__(model)
        self.resource = Template


class Template(Resource):
    def __init__(self, model, ident):
        super(Template, self).__init__(model, ident)
        self.update_params = ["name", "folder", "icon", "os_distro",
                              "storagepool", "os_version", "cpus",
                              "memory", "cdrom", "disks"]
        self.uri_fmt = "/templates/%s"

    @property
    def data(self):
        return {'name': self.ident,
                'icon': self.info['icon'],
                'os_distro': self.info['os_distro'],
                'os_version': self.info['os_version'],
                'cpus': self.info['cpus'],
                'memory': self.info['memory'],
                'cdrom': self.info['cdrom'],
                'disks': self.info['disks'],
                'storagepool': self.info['storagepool'],
                'folder': self.info.get('folder', [])}


class Interfaces(Collection):
    def __init__(self, model):
        super(Interfaces, self).__init__(model)
        self.resource = Interface


class Interface(Resource):
    def __init__(self, model, ident):
        super(Interface, self).__init__(model, ident)
        self.uri_fmt = "/interfaces/%s"

    @property
    def data(self):
        return {'name': self.ident,
                'type': self.info['type'],
                'ipaddr': self.info['ipaddr'],
                'netmask': self.info['netmask'],
                'status': self.info['status']}


class Networks(Collection):
    def __init__(self, model):
        super(Networks, self).__init__(model)
        self.resource = Network


class Network(Resource):
    def __init__(self, model, ident):
        super(Network, self).__init__(model, ident)
        self.uri_fmt = "/networks/%s"
        self.activate = self.generate_action_handler('activate')
        self.deactivate = self.generate_action_handler('deactivate')

    @property
    def data(self):
        return {'name': self.ident,
                'autostart': self.info['autostart'],
                'connection': self.info['connection'],
                'interface': self.info['interface'],
                'subnet': self.info['subnet'],
                'dhcp': self.info['dhcp'],
                'state': self.info['state']}


class StorageVolume(Resource):
    def __init__(self, model, pool, ident):
        super(StorageVolume, self).__init__(model, ident)
        self.pool = pool
        self.ident = ident
        self.info = {}
        self.model_args = [self.pool, self.ident]
        self.uri_fmt = '/storagepools/%s/storagevolumes/%s'
        self.resize = self.generate_action_handler('resize', ['size'])
        self.wipe = self.generate_action_handler('wipe')

    @property
    def data(self):
        res = {'name': self.ident,
               'type': self.info['type'],
               'capacity': self.info['capacity'],
               'allocation': self.info['allocation'],
               'path': self.info['path'],
               'format': self.info['format']}
        for key in ('os_version', 'os_distro', 'bootable'):
            val = self.info.get(key)
            if val:
                res[key] = val
        return res


class IsoVolumes(Collection):
    def __init__(self, model, pool):
        super(IsoVolumes, self).__init__(model)
        self.pool = pool

    def get(self):
        res_list = []
        try:
            get_list = getattr(self.model, model_fn(self, 'get_list'))
            res_list = get_list(*self.model_args)
        except AttributeError:
            pass
        return kimchi.template.render(get_class_name(self), res_list)


class StorageVolumes(Collection):
    def __init__(self, model, pool):
        super(StorageVolumes, self).__init__(model)
        self.resource = StorageVolume
        self.pool = pool
        self.resource_args = [self.pool, ]
        self.model_args = [self.pool, ]


class StoragePool(Resource):
    def __init__(self, model, ident):
        super(StoragePool, self).__init__(model, ident)
        self.update_params = ["autostart"]
        self.uri_fmt = "/storagepools/%s"
        self.activate = self.generate_action_handler('activate')
        self.deactivate = self.generate_action_handler('deactivate')

    @property
    def data(self):
        res = {'name': self.ident,
               'state': self.info['state'],
               'capacity': self.info['capacity'],
               'allocated': self.info['allocated'],
               'available': self.info['available'],
               'path': self.info['path'],
               'source': self.info['source'],
               'type': self.info['type'],
               'nr_volumes': self.info['nr_volumes'],
               'autostart': self.info['autostart']}
        val = self.info.get('task_id')
        if val:
            res['task_id'] = val
        return res


    def _cp_dispatch(self, vpath):
        if vpath:
            subcollection = vpath.pop(0)
            if subcollection == 'storagevolumes':
                # incoming text, from URL, is not unicode, need decode
                return StorageVolumes(self.model, self.ident.decode("utf-8"))


class IsoPool(Resource):
    def __init__(self, model):
        super(IsoPool, self).__init__(model, ISO_POOL_NAME)

    @property
    def data(self):
        return {'name': self.ident,
                'state': self.info['state'],
                'type': self.info['type']}

    def _cp_dispatch(self, vpath):
        if vpath:
            subcollection = vpath.pop(0)
            if subcollection == 'storagevolumes':
                # incoming text, from URL, is not unicode, need decode
                return IsoVolumes(self.model, self.ident.decode("utf-8"))


class StoragePools(Collection):
    def __init__(self, model):
        super(StoragePools, self).__init__(model)
        self.resource = StoragePool
        isos = IsoPool(model)
        isos.exposed = True
        setattr(self, ISO_POOL_NAME, isos)

    def create(self, *args):
        try:
            create = getattr(self.model, model_fn(self, 'create'))
        except AttributeError:
            raise cherrypy.HTTPError(405,
                'Create is not allowed for %s' % get_class_name(self))
        params = parse_request()
        args = self.model_args + [params]
        name = create(*args)
        args = self.resource_args + [name]
        res = self.resource(self.model, *args)
        resp = res.get()
        if 'task_id' in res.data:
            cherrypy.response.status = 202
        else:
            cherrypy.response.status = 201
        return resp

    def _get_resources(self):
        try:
            res_list = super(StoragePools, self)._get_resources()
            # Append reserved pools
            isos = getattr(self, ISO_POOL_NAME)
            isos.lookup()
            res_list.append(isos)
        except AttributeError:
            pass
        return res_list

class Task(Resource):
    def __init__(self, model, id):
        super(Task, self).__init__(model, id)

    @property
    def data(self):
        return {'id': self.ident,
                'status': self.info['status'],
                'message': self.info['message']}


class Tasks(Collection):
    def __init__(self, model):
        super(Tasks, self).__init__(model)
        self.resource = Task


class DebugReports(AsyncCollection):
    def __init__(self, model):
        super(DebugReports, self).__init__(model)
        self.resource = DebugReport


class DebugReport(Resource):
    def __init__(self, model, ident):
        super(DebugReport, self).__init__(model, ident)
        self.ident = ident
        self.content = DebugReportContent(model, ident)

    @property
    def data(self):
        return {'name': self.ident,
                'file': self.info['file'],
                'time': self.info['ctime']}


class Config(Resource):
    def __init__(self, model, id=None):
        super(Config, self).__init__(model, id)
        self.capabilities = Capabilities(self.model)
        self.capabilities.exposed = True
        self.distros = Distros(model)
        self.distros.exposed = True

    @property
    def data(self):
        return {'http_port': cherrypy.server.socket_port}

class Capabilities(Resource):
    def __init__(self, model, id=None):
        super(Capabilities, self).__init__(model, id)
        self.model = model

    @property
    def data(self):
        caps = ['libvirt_stream_protocols', 'qemu_stream',
                'screenshot', 'system_report_tool']
        ret = dict([(x, None) for x in caps])
        ret.update(self.model.get_capabilities())
        return ret


class Distro(Resource):
    def __init__(self, model, ident):
        super(Distro, self).__init__(model, ident)

    @property
    def data(self):
        return self.info


class Distros(Collection):
    def __init__(self, model):
        super(Distros, self).__init__(model)
        self.resource = Distro


class Host(Resource):
    def __init__(self, model, id=None):
        super(Host, self).__init__(model, id)
        self.stats = HostStats(self.model)
        self.stats.exposed = True
        self.uri_fmt = '/host/%s'
        self.reboot = self.generate_action_handler('reboot')
        self.shutdown = self.generate_action_handler('shutdown')
        self.partitions = Partitions(self.model)
        self.partitions.exposed = True

    @property
    def data(self):
        return self.info

class HostStats(Resource):
    @property
    def data(self):
        return self.info

class Partitions(Collection):
    def __init__(self, model):
        super(Partitions, self).__init__(model)
        self.resource = Partition


class Partition(Resource):
    def __init__(self, model,id):
        super(Partition, self).__init__(model,id)

    @property
    def data(self):
        return self.info

@cherrypy.expose
def login(*args):
    params = parse_request()
    try:
        userid = params['userid']
        password = params['password']
    except KeyError, key:
        raise cherrypy.HTTPError(400, "Missing parameter: '%s'" % key)
    try:
        auth.login(userid, password)
    except OperationFailed:
        raise cherrypy.HTTPError(401)
    return '{}'

@cherrypy.expose
def logout():
    auth.logout()
    return '{}'

class Plugins(Collection):
    def __init__(self, model):
        super(Plugins, self).__init__(model)
        self.model = model

    @property
    def data(self):
        return self.info

    def get(self):
        res_list = []
        try:
            get_list = getattr(self.model, model_fn(self, 'get_list'))
            res_list = get_list(*self.model_args)
        except AttributeError:
            pass
        return kimchi.template.render(get_class_name(self), res_list)
