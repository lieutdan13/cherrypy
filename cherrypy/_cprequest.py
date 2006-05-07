"""CherryPy core request/response handling."""

import Cookie
import os
import sys
import types

import cherrypy
from cherrypy import _cperror, _cputil, _cpcgifs, tools
from cherrypy.lib import cptools, httptools


class Request(object):
    """An HTTP request."""
    
    def __init__(self, remote_addr, remote_port, remote_host, scheme="http"):
        """Populate a new Request object.
        
        remote_addr should be the client IP address
        remote_port should be the client Port
        remote_host should be string of the client's IP address.
        scheme should be a string, either "http" or "https".
        """
        self.remote_addr = remote_addr
        self.remote_port = remote_port
        self.remote_host = remote_host
        
        self.scheme = scheme
        self.closed = False
        
        pts = ['on_start_resource', 'before_request_body',
               'before_main', 'before_finalize',
               'on_end_resource', 'on_end_request',
               'before_error_response', 'after_error_response']
        self.hooks = tools.HookMap(pts)
        self.hooks.failsafe = ['on_start_resource', 'on_end_resource',
                               'on_end_request']
    
    def close(self):
        if not self.closed:
            self.closed = True
            self.hooks.run('on_end_request')
            cherrypy.serving.__dict__.clear()
    
    def run(self, request_line, headers, rfile):
        """Process the Request.
        
        request_line should be of the form "GET /path HTTP/1.0".
        headers should be a list of (name, value) tuples.
        rfile should be a file-like object containing the HTTP request
            entity.
        
        When run() is done, the returned object should have 3 attributes:
          status, e.g. "200 OK"
          headers, a list of (name, value) tuples
          body, an iterable yielding strings
        
        Consumer code (HTTP servers) should then access these response
        attributes to build the outbound stream.
        
        """
        self.log_access = _cputil.log_access
        self.error_response = cherrypy.HTTPError(500).set_response
        
        self.request_line = request_line.strip()
        self.header_list = list(headers)
        self.rfile = rfile
        self.headers = httptools.HeaderMap()
        self.simple_cookie = Cookie.SimpleCookie()
        
        if cherrypy.profiler:
            cherrypy.profiler.run(self._run)
        else:
            self._run()
        
        if self.method == "HEAD":
            # HEAD requests MUST NOT return a message-body in the response.
            cherrypy.response.body = []
        
        self.log_access()
        
        return cherrypy.response
    
    def _run(self):
        try:
            # This has to be done very early in the request process,
            # because request.object_path is used for config lookups
            # right away.
            self.process_request_line()
            self.dispatch = self.config.get("dispatch") or _cputil.dispatch
            self.hooks.setup()
            
            try:
                self.hooks.run('on_start_resource')
                
                try:
                    self.process_headers()
                    
                    self.hooks.run('before_request_body')
                    if self.process_request_body:
                        self.process_body()
                    
                    # Loop to allow for InternalRedirect.
                    while True:
                        try:
                            self.hooks.run('before_main')
                            if self.dispatch:
                                self.dispatch(self.object_path)
                            break
                        except cherrypy.InternalRedirect, ir:
                            self.object_path = ir.path
                    
                    self.hooks.run('before_finalize')
                    cherrypy.response.finalize()
                except (cherrypy.HTTPRedirect, cherrypy.HTTPError), inst:
                    # For an HTTPRedirect or HTTPError (including NotFound),
                    # we don't go through the regular mechanism:
                    # we return the redirect or error page immediately
                    inst.set_response()
                    self.hooks.run('before_finalize')
                    cherrypy.response.finalize()
            finally:
                self.hooks.run('on_end_resource')
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            if cherrypy.config.get("server.throw_errors", False):
                raise
            self.handle_error(sys.exc_info())
    
    def _get_object_path(self):
        return self._object_path
    def _set_object_path(self, value):
        self._object_path = value
        self.config = cherrypy.config.request_config()
        
        # Get all 'tools.*' config entries as a {toolname: {k: v}} dict.
        self.toolmap = {}
        for k, v in self.config.iteritems():
            atoms = k.split(".")
            namespace = atoms.pop(0)
            if namespace == "tools":
                toolname = atoms.pop(0)
                bucket = self.toolmap.setdefault(toolname, {})
                bucket[".".join(atoms)] = v
    object_path = property(_get_object_path, _set_object_path,
                           doc="The path to the rendered resource.")
    
    def process_request_line(self):
        """Parse the first line (e.g. "GET /path HTTP/1.1") of the request."""
        rl = self.request_line
        method, path, qs, proto = httptools.parse_request_line(rl)
        if path == "*":
            path = "global"
        
        self.method = method
        self.process_request_body = method in ("POST", "PUT")
        
        self.path = path
        self.query_string = qs
        self.protocol = proto
        
        # Change object_path to change the object that will get rendered
        self.object_path = path
        
        # Compare request and server HTTP versions, in case our server does
        # not support the requested version. We can't tell the server what
        # version number to write in the response, so we limit our output
        # to min(req, server). We want the following output:
        #     request    server     actual written   supported response
        #     version    version   response version  feature set (resp.v)
        # a     1.0        1.0           1.0                1.0
        # b     1.0        1.1           1.1                1.0
        # c     1.1        1.0           1.0                1.0
        # d     1.1        1.1           1.1                1.1
        # Notice that, in (b), the response will be "HTTP/1.1" even though
        # the client only understands 1.0. RFC 2616 10.5.6 says we should
        # only return 505 if the _major_ version is different.
        
        # cherrypy.request.version == request.protocol in a Version instance.
        self.version = httptools.Version.from_http(self.protocol)
        
        # cherrypy.response.version should be used to determine whether or
        # not to include a given HTTP/1.1 feature in the response content.
        server_v = cherrypy.config.get("server.protocol_version", "HTTP/1.0")
        server_v = httptools.Version.from_http(server_v)
        cherrypy.response.version = min(self.version, server_v)
    
    def process_headers(self):
        self.params = httptools.parseQueryString(self.query_string)
        
        # Process the headers into self.headers
        for name, value in self.header_list:
            value = value.strip()
            # Warning: if there is more than one header entry for cookies (AFAIK,
            # only Konqueror does that), only the last one will remain in headers
            # (but they will be correctly stored in request.simple_cookie).
            self.headers[name] = value
            
            # Handle cookies differently because on Konqueror, multiple
            # cookies come on different lines with the same key
            if name.title() == 'Cookie':
                self.simple_cookie.load(value)
        
        if self.version >= "1.1":
            # All Internet-based HTTP/1.1 servers MUST respond with a 400
            # (Bad Request) status code to any HTTP/1.1 request message
            # which lacks a Host header field.
            if not self.headers.has_key("Host"):
                msg = "HTTP/1.1 requires a 'Host' request header."
                raise cherrypy.HTTPError(400, msg)
        self.base = "%s://%s" % (self.scheme, self.headers.get('Host', ''))
    
    def _get_browser_url(self):
        url = self.base + self.path
        if self.query_string:
            url += '?' + self.query_string
        return url
    browser_url = property(_get_browser_url,
                          doc="The URL as entered in a browser (read-only).")
    
    def process_body(self):
        """Convert request.rfile into request.params (or request.body)."""
        # Create a copy of headers with lowercase keys because
        # FieldStorage doesn't work otherwise
        lowerHeaderMap = {}
        for key, value in self.headers.items():
            lowerHeaderMap[key.lower()] = value
        
        # FieldStorage only recognizes POST, so fake it.
        methenv = {'REQUEST_METHOD': "POST"}
        try:
            forms = _cpcgifs.FieldStorage(fp=self.rfile,
                                          headers=lowerHeaderMap,
                                          environ=methenv,
                                          keep_blank_values=1)
        except httptools.MaxSizeExceeded:
            # Post data is too big
            raise cherrypy.HTTPError(413)
        
        if forms.file:
            # request body was a content-type other than form params.
            self.body = forms.file
        else:
            self.params.update(httptools.paramsFromCGIForm(forms))
    
    def handle_error(self, exc):
        response = cherrypy.response
        try:
            self.hooks.run("before_error_response")
            if self.error_response:
                self.error_response()
            self.hooks.run("after_error_response")
            response.finalize()
            return
        except cherrypy.HTTPRedirect, inst:
            try:
                inst.set_response()
                response.finalize()
                return
            except (KeyboardInterrupt, SystemExit):
                raise
            except:
                # Fall through to the second error handler
                pass
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            # Fall through to the second error handler
            pass
        
        # Failure in error handler or finalize. Bypass them.
        if cherrypy.config.get('server.show_tracebacks', False):
            dbltrace = ("\n===First Error===\n\n%s"
                        "\n\n===Second Error===\n\n%s\n\n")
            body = dbltrace % (_cperror.format_exc(exc),
                               _cperror.format_exc())
        else:
            body = ""
        r = _cperror.bare_error(body)
        response.status, response.header_list, response.body = r


class Body(object):
    """The body of the HTTP response (the response entity)."""
    
    def __get__(self, obj, objclass=None):
        if obj is None:
            # When calling on the class instead of an instance...
            return self
        else:
            return obj._body
    
    def __set__(self, obj, value):
        # Convert the given value to an iterable object.
        if isinstance(value, types.FileType):
            value = cptools.fileGenerator(value)
        elif isinstance(value, types.GeneratorType):
            value = flattener(value)
        elif isinstance(value, basestring):
            # strings get wrapped in a list because iterating over a single
            # item list is much faster than iterating over every character
            # in a long string.
            value = [value]
        elif value is None:
            value = []
        obj._body = value


def flattener(input):
    """Yield the given input, recursively iterating over each result (if needed)."""
    for x in input:
        if not isinstance(x, types.GeneratorType):
            yield x
        else:
            for y in flattener(x):
                yield y 


class Response(object):
    """An HTTP Response."""
    
    body = Body()
    
    def __init__(self):
        self.status = None
        self.header_list = None
        self.body = None
        
        self.headers = httptools.HeaderMap()
        content_type = cherrypy.config.get('server.default_content_type', 'text/html')
        self.headers.update({
            "Content-Type": content_type,
            "Server": "CherryPy/" + cherrypy.__version__,
            "Date": httptools.HTTPDate(),
            "Set-Cookie": [],
            "Content-Length": None
        })
        self.simple_cookie = Cookie.SimpleCookie()
    
    def collapse_body(self):
        newbody = ''.join([chunk for chunk in self.body])
        self.body = newbody
        return newbody
    
    def finalize(self):
        """Transform headers (and cookies) into cherrypy.response.header_list."""
        
        try:
            code, reason, _ = httptools.validStatus(self.status)
        except ValueError, x:
            raise cherrypy.HTTPError(500, x.args[0])
        
        self.status = "%s %s" % (code, reason)
        
        stream = cherrypy.config.get("stream_response", False)
        # OPTIONS requests MUST include a Content-Length of 0 if no body.
        # Just punt and figure Content-Length for all OPTIONS requests.
        if cherrypy.request.method == "OPTIONS":
            stream = False
        
        if stream:
            try:
                del self.headers['Content-Length']
            except KeyError:
                pass
        else:
            # Responses which are not streamed should have a Content-Length,
            # but allow user code to set Content-Length if desired.
            if self.headers.get('Content-Length') is None:
                content = self.collapse_body()
                self.headers['Content-Length'] = len(content)
        
        # Transform our header dict into a sorted list of tuples.
        self.header_list = self.headers.sorted_list()
        
        cookie = self.simple_cookie.output()
        if cookie:
            for line in cookie.split("\n"):
                name, value = line.split(": ", 1)
                self.header_list.append((name, value))