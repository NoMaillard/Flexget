from __future__ import unicode_literals, division, absolute_import
import logging
import threading
import base64
import hashlib
import random

from sqlalchemy import Column, Integer, Unicode

from flask import Flask, abort, redirect
from flask.ext.login import UserMixin

from flexget.event import event
from flexget.config_schema import register_config_key
from flexget.utils.tools import singleton
from flexget.manager import Base
from flexget.utils.database import with_session

log = logging.getLogger('web_server')

_home = None
_app_register = {}
_default_app = Flask(__name__)

random = random.SystemRandom()

web_config_schema = {
    'oneOf': [
        {'type': 'boolean'},
        {
            'type': 'object',
            'properties': {
                'bind': {'type': 'string', 'format': 'ipv4', 'default': '0.0.0.0'},
                'port': {'type': 'integer', 'default': 3539},
            },
            'additionalProperties': False
        }
    ]
}


def generate_key():
    """ Generate key for use to authentication """
    return hashlib.sha224(str(random.getrandbits(128))).hexdigest()


def get_random_string(length=12, allowed_chars='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'):
    """
    Returns a securely generated random string.

    The default length of 12 with the a-z, A-Z, 0-9 character set returns
    a 71-bit value. log_2((26+26+10)^12) =~ 71 bits.

    Taken from the django.utils.crypto module.
    """
    return ''.join(random.choice(allowed_chars) for __ in range(length))


@with_session
def get_secret(session=None):
    pass
    """ Generate a secret key for flask applications and store it in the database. """
    web_secret = session.query(WebSecret).first()
    if not web_secret:
        web_secret = WebSecret(id=1, value=get_random_string(50, 'abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)'))
        session.add(web_secret)
        session.commit()

    return web_secret.value


class User(Base, UserMixin):
    """ User class avaliable for flask apps to handle authentication using flask_login """
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    name = Column(Unicode(50), unique=True)
    token = Column(Unicode, default=generate_key)
    password = Column(Unicode)

    def __repr__(self):
        return '<User %r>' % self.name

    def get_id(self):
        return self.name


class WebSecret(Base):
    """ Store flask secret in the database """
    __tablename__ = 'secret'

    id = Column(Unicode, primary_key=True)
    value = Column(Unicode)


@event('config.register')
def register_config():
    register_config_key('web_server', web_config_schema)


def register_app(path, application):
    if path in _app_register:
        raise ValueError('path %s already registered')
    _app_register[path] = application


def register_home(route):
    """Registers UI home page"""
    global _home
    _home = route


@_default_app.route('/')
def start_page():
    """ Redirect user to registered UI home """
    if not _home:
        abort(404)
    return redirect(_home)


@event('manager.daemon.started', -255)  # Low priority so plugins can register apps
@with_session
def setup_server(manager, session=None):
    """ Sets up and starts/restarts the web service. """
    if not manager.is_daemon:
        return

    web_server_config = manager.config.get('web_server')

    if not web_server_config:
        return

    web_server = WebServer(
        bind=web_server_config['bind'],
        port=web_server_config['port'],
    )

    _default_app.secret_key = get_secret()

    # Create default flexget user
    if session.query(User).count() == 0:
        session.add(User(name="flexget", password="flexget"))
        session.commit()

    if web_server.is_alive():
        web_server.stop()

    if _app_register:
        web_server.start()


@event('manager.shutdown_requested')
def stop_server(manager):
    """ Sets up and starts/restarts the webui. """
    if not manager.is_daemon:
        return
    web_server = WebServer()
    if web_server.is_alive():
        web_server.stop()


@singleton
class WebServer(threading.Thread):
    # We use a regular list for periodic jobs, so you must hold this lock while using it
    triggers_lock = threading.Lock()

    def __init__(self, bind='0.0.0.0', port=5050):
        threading.Thread.__init__(self, name='web_server')
        self.bind = str(bind)  # String to remove unicode warning from cherrypy startup
        self.port = port
        self.server = None

    def start(self):
        # If we have already started and stopped a thread, we need to reinitialize it to create a new one
        if not self.is_alive():
            self.__init__(bind=self.bind, port=self.port)
        threading.Thread.start(self)

    def _start_server(self):
        from cherrypy import wsgiserver

        apps = {'/': _default_app}
        for path, registered_app in _app_register.iteritems():
            apps[path] = registered_app

        d = wsgiserver.WSGIPathInfoDispatcher(apps)
        self.server = wsgiserver.CherryPyWSGIServer((self.bind, self.port), d)
        self.server.start()

    def run(self):
        log.info('Starting web server on port %s' % self.port)
        self._start_server()

    def stop(self):
        log.info('Shutting down web server')
        if self.server:
            self.server.stop()