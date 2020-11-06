#!/bin/env python3

import boto3
import jinja2
import logging
import os
import re
import redis
import shutil
import subprocess
import sys

from urllib.parse import urlparse, parse_qs
from time import sleep

# Custom exceptions
class ConfigurationError(Exception):
    '''
    Simple error to allow Retemplate to raise problems related to its own config
    '''
    def __init__(self, reason):
        self.reason = reason


# Value stores
class DataStore(object):
    '''
    DataStore is essentially an interface for specific methods of accessing
    data.

    Arguments:
        name (str): An arbitrary name for the DataStore
    '''
    def __init__(self, name, *args, **kwargs):
        self.name = name

    def get_value(self, key):
        raise NotImplementedError


class AwsSecretsManagerStore(DataStore):
    '''
    A DataStore to fetch secrets from AWS Secrets Manager. Arguments for this constructor map to the
    [boto3 client documentation]
    (https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html#boto3.session.Session.client).
    '''
    def __init__(self, name, *args, **kwargs):
        super().__init__(self, name, *args, **kwargs)
        # boto won't like our "type" key, so del it
        if 'type' in kwargs: del kwargs['type']
        self.client = boto3.client('secretsmanager', **kwargs)

    def get_value(self, key, **kwargs):
        '''
        Retrieves the value of a AWS Secrets Manager secret

        Arguments:
            key (str): The SecretId of the secret. If you supply an explicit SecretId as part of
                additional keyword arguments, this argument will be ignored.

        '''
        if 'SecretId' not in kwargs:
            kwargs['SecretId'] = key
        return self.client.get_secret_value(**kwargs)['SecretString']


class RedisStore(DataStore):
    '''
    A RedisStore is a DataStore implementation that uses a Redis backend

    Arguments:
        name (str): An arbitrary name for the store, like 'local-redis' (default: 'redis')
        host (str): Hostname to connect to (default: 'localhost')
        port (int): The port Redis runs on at that host (default: 6379)
        db (int): The logical database ID to use (default: 0)
        auth_token (str): The password to use with AUTH (default: None)
        ssl (bool): Whether or not to encrypt traffic to the Redis store (default: False)
    '''

    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.settings = {
            'host': 'localhost',
            'port': 6379,
            'db': 0,
            'auth_token': None,
            'ssl': False
        }
        self.settings.update(kwargs)
        self.client = redis.Redis(
            host=self.settings['host'],
            port=self.settings['port'],
            ssl=self.settings['ssl'],
            password=self.settings['auth_token'])

    def get_value(self, key):
        logging.debug('RedisStore {} getting key {}'.format(self.name, key))
        return self.client.get(key)


class Retemplate(object):
    '''
    A class representing a single template and operations surrounding it.

    Arguments:
        target (str): The file that will ultimately be managed by the template (required)
        template (str): The file where the source template is found (required)
        stores (list): A list of DataStore objects that the template may need to resolve "rtpl" URIs
        owner (str): The user owner to set for the target
        group (str): The group owner to set for the target
        onchange (str): A command to run when the target file changes
        frequency (int): The number of seconds to wait between executions of the template
    '''

    def __init__(self, target, template, stores, **kwargs):
        self.target = target
        self.template = template
        self.stores = stores
        self.settings = {
            'owner': None,
            'group': None,
            'chmod': 600,
            'onchange': None,
            'frequency': 60
        }
        self.settings.update(kwargs)

    def resolve_value(self, uri):
        '''
        Breaks down the given URI to identify a DataStore and retrieve a value from it

        Arguments:
            uri (str): A Retemplate URI in the form of
                `rtpl://datastore-name/key-name?param1=val1&paramN=valN`
        '''

        if not uri.startswith('rtpl://'):
            raise ConfigurationError('Malformed retemplate URI: {}'.format(uri))

        url = urlparse(uri)
        qs = parse_qs(url.query)

        # qs looks like this, with lists for values:
        # {'param1': ['val1'], 'paramN': ['valN']}
        # So just cat them together
        for q in qs:
            qs[q] = ''.join(qs[q])

        store = self.stores[url.netloc]
        key = url.path[1:] # path always starts with a '/'; cut it out
        value = store.get_value(key, **qs)
        logging.debug('Store {} got value \'{}\' for key \'{}\''.format(store.name, value, key))
        return value

    def render(self):
        '''
        Renders a template in two phases. First, "rtpl" URIs are resolved (see resolve_value). Then,
        the template is passed through the Jinja interpreter. The final text is returned.
        '''

        # Look for prerender values that match this URI format:
        # rtpl://type?param1=val1&param2=val2
        logging.info('Rendering template {} for target {}'.format(self.template, self.target))
        try:
            with open(self.template, 'r') as fh:
                tpl = fh.read()
        except IOError:
            logging.error('Cannot access template file {} for target {}'.format(
                self.template, self.target))
            return False

        tpl = tpl.split('\n')
        prerender = list()
        for line in tpl:
            match = re.search('(rtpl://.*)\s?', line)
            if match:
                groups = match.groups()
                for group in groups:
                    value = self.resolve_value(group)
                    if type(value) == bytes:
                        value = value.decode()
                    prerender.append(line.replace(group, value))
        prerender = '\n'.join(prerender)
        return jinja2.Template(prerender).render()

    def write_file(self, content):
        '''
        Writes `content` to the target file, then sets ownership and mode for the file.

        Arguments:
            content (str): The data to write to the target file
        '''

        try:
            with open(self.target, 'w') as fh:
                fh.write(content)
            shutil.chown(self.target, user=self.settings['owner'], group=self.settings['group'])
            os.chmod(self.target, self.settings['chmod'])
            return True
        except IOError:
            logging.error('Cannot write target file {}'.format(self.target))
            return False

    def execute_onchange(self):
        '''
        Runs the "onchange" command for this template. Prints the exit code and stdout as debug
        output.
        '''

        onchange = self.settings['onchange']
        # Why would someone not set an onchange command? I dunno, but we should handle it gracefully
        if onchange is None:
            logging.info('No onchange command set for target {}'.format(self.target))
            return

        logging.info('Running onchange command \'{}\' for target {}'.format(onchange, self.target))
        try:
            proc = subprocess.run(onchange.split(' '), capture_output=True)
            logging.debug('onchange command exited: {}'.format(proc.returncode))
            logging.debug('onchange command output: {}'.format(proc.stdout))
        except subprocess.CalledProcessError as ex:
            logging.error('[{}] Couldn\'t call process {}'.format(target, onchange))
            logging.error(ex)

    def run(self):
        '''
        Runs an infinite loop of the following steps:

        - Read in the template file and render it
        - Check if the new file content differs from what's on disk in the target file. If so...
            - Write the new file
            - Update its ownership and mode
            - Run the onchange command
        - Delay by the configured frequency value
        - Rinse, repeat
        '''

        while True:
            new_version = self.render()
            if not new_version:
                break
            try:
                with open(self.target, 'r') as fh:
                    current_version = fh.read()
            except IOError:
                logging.error('Cannot read target file {}'.format(self.target))
                break

            if new_version != current_version:
                logging.info('New version of target {} detected'.format(self.target))
                if not self.write_file(new_version):
                    break
                self.execute_onchange()
            else:
                logging.info('Target {} is unchanged'.format(self.target))

            logging.info('Waiting {} seconds to run target {} again'.format(
                self.settings['frequency'], self.target))
            sleep(self.settings['frequency'])
