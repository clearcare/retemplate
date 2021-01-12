#!/bin/env python3

import boto3
import jinja2
import logging
import os
import random
import re
import redis
import requests
import shutil
import subprocess
import sys

from botocore.exceptions import ClientError
from urllib.parse import urlparse, parse_qs
from time import sleep

# Custom exceptions
class RetemplateError(Exception):
    pass


class ConfigurationError(RetemplateError):
    '''
    Simple error to allow Retemplate to raise problems related to its own config
    '''

    def __init__(self, reason):
        self.reason = reason


class RetrievalError(RetemplateError):
    '''
    Exception for anytime get_value throws an exception
    '''
    pass


class RenderError(RetemplateError):
    '''
    Used when some aspect of the rendering process fails
    '''
    pass

# Data stores
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


class AwsLocalMetadataServer(DataStore):
    '''
    A DataStore to get data from the local metadata server that runs on AWS instances
    '''

    def __init__(self, name, *args, **kwargs):
        super().__init__(self, name, *args, **kwargs)

    def get_value(self, key, **kwargs):
        '''
        Retrieves the response body of a call to the AWS local metadata server, or an empty string
        if no such value exists.

        Arguments:
            key(str): The path to make the request to
        '''

        resp = requests.get('http://169.254.169.254/{}'.format(key))
        if resp.status_code == 200:
            return resp.text
        else:
            logging.error('Got response code {} from AWS local metadata server'.format(resp.status_code))
            raise RetrievalError


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
        try:
            return self.client.get_secret_value(**kwargs)['SecretString']
        except ClientError as ex:
            logging.error('Failed to retrieve secret {}; Error code: {}; Full error: {}'.format(
                kwargs['SecretId'], ex.response['Error']['Code'], ex))
            raise RetrievalError


class AwsSystemsManagerStore(DataStore):
    '''
    A DataStore to fetch secrets from AWS Systems Manager. Arguments for this constructor map to the
    [boto3 client documentation]
    (https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html#boto3.session.Session.client).
    '''

    def __init__(self, name, *args, **kwargs):
        super().__init__(self, name, *args, **kwargs)
        # boto won't like our "type" key, so del it
        if 'type' in kwargs: del kwargs['type']
        self.client = boto3.client('ssm', **kwargs)

    def get_value(self, key, **kwargs):
        '''
        Retrieves the value of a AWS Systems Manager secret

        Arguments:
            key (str): The Name of the parameter. If you supply an explicit Name as part of
                additional keyword arguments, this argument will be ignored.

        '''

        # Parameter store key names begin with a slash, but the URI parser will
        # strip that out because nothing else uses it. You can avoid this by starting
        # parameter store paths with an extra slash, but here we'll add it back in
        # just in case.
        if key[0] != '/':
            key = '/' + key

        # "Name" is how SSM does parameter lookups. You should just use the key, but
        # if you are so inclined, you can override it in the query string.
        if 'Name' not in kwargs:
            kwargs['Name'] = key
        try:
            return self.client.get_parameter(**kwargs)['Parameter']['Value']
        except ClientError as ex:
            logging.error('Failed to retrieve parameter {}; Error code: {}; Full error: {}'.format(
                kwargs['Name'], e.response['Error']['Code'], e))
            raise RetrievalError


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
        try:
            return self.client.get(key)
        except redis.exceptions.RedisError as ex:
            logging.error('Failed to get Redis key {}; error: {}'.format(key, ex))
        raise RetrievalError


class LocalExecutionStore(DataStore):
    '''
    A DataStore that issues a terminal command and uses its standard output as a template value.
    '''

    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        try:
            self.command = kwargs['command']
        except IndexError:
            logging.error(
                'Cannot register local-exec store {} because no command has been supplied'\
                .format(name))

    def get_value(self, key, **kwargs):
        try:
            subp_args = [ self.command ]
            subp_args.extend(kwargs['arg'])

            # Clean these args up a bit
            for i in range(0, len(subp_args)):
                subp_args[i] = subp_args[i].strip()

            proc = subprocess.run(subp_args, capture_output=True)
            output = proc.stdout.decode('utf-8').strip()
            return output
        except Exception as ex:
            logging.error('Failed to get local execution data. Error: {}'.format(ex))
        raise RetrievalError


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

    def __init__(self, target, template, stores, display_values, **kwargs):
        self.target = target
        self.template = template
        self.stores = stores
        self.display_values = display_values
        self.settings = {
            'owner': None,
            'group': None,
            'chmod': 600,
            'onchange': None,
            'frequency': 60,
            'random_offset_max': None
        }
        self.settings.update(kwargs)
        self.vars = dict()

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

        store = self.stores[url.netloc]
        key = url.path[1:].strip() # path always starts with a '/'; cut it out
        try:
            value = store.get_value(key, **qs)
            if self.display_values:
                logging.info('Store {} got value \'{}\' for key \'{}\' and query \'{}\''.format(
                    store.name, value, key, url.query))
        except RetrievalError:
            logging.error('Failed to resolve value for URI: {}'.format(uri))
            raise
        return value

    def read_template(self):
        try:
            with open(self.template, 'r') as fh:
                return fh.read()
        except IOError:
            logging.error('Cannot access template file {} for target {}'.format(
                self.template, self.target))
            raise

    def preprocess(self, tpl):
        '''
        Scan the template looking for lines with this kind of variable assignment in it:

            {<message = rtpl://something>}

        Then look for places where the variable gets used like this...

            {<message>}

        ...and replace those with the actual value.
        '''

        lines = tpl.split('\n')
        stage_one = list()
        stage_two = list()

        for i in range(0, len(lines)):
            # Assignments must be the only thing on the line
            match = re.search('^{<\s+([a-zA-Z0-9_]*)\s+=\s+(rtpl://.*)>}$', lines[i])
            if match:
                groups = match.groups()
                if groups[1].startswith('rtpl://'):
                    self.vars[groups[0]] = self.resolve_value(groups[1])
                else:
                    self.vars[groups[0]] = groups[1]
                # Now update all future lines so they get parsed right
                for j in range(i, len(lines)):
                    for var in self.vars:
                        lines[j] = lines[j].replace('{{<{}>}}'.format(var), self.vars[var])
            else:
                stage_one.append(lines[i])


        return '\n'.join(stage_one)

    def process(self, tpl):
        '''
        Look for "rtpl://" URIs and replace them with the values they reference.
        '''

        tpl = tpl.split('\n')
        prerender = list()
        for line in tpl:
            match = re.search('(rtpl://[a-zA-Z0-9-_/=?&.]*)', line)
            if match:
                groups = match.groups()
                for group in groups:
                    value = self.resolve_value(group)
                    if type(value) == bytes:
                        value = value.decode()
                    prerender.append(line.replace(group, value))
            else:
                prerender.append(line)
        return '\n'.join(prerender)

    def render(self):
        '''
        Renders a template in three phases:
            1. The template is "preprocessed" looking for special variable assignments that look
               like this: {<varname = rtpl://source/key>}. These are resolved and the variables
               stored internally. Then, references to that variable are replaced with the value.
               Variable references look like this: {<varname>}
            2. Ordinary rtpl URIs are resolved.
            3. The template is passed through the Jinja interpreter.
        The final text is returned.

        This three-phase approach allows you to build more complex lookups. For example, suppose you
        need to look up the name of the environment the template is being run in with a local-exec
        call, then later need to look up an AWS Secrets Manager value using that environment name.
        You might have to do something like this:

            {<env = rtpl://tag-finder/environment>}
            password: rtpl://secrets/{<env>}.the_password
        '''

        # Optionally stagger renders to avoid "thundering herd" effect when running this from many hosts
        if self.settings.get('random_offset_max'):
            offset = random.randint(1, self.settings['random_offset_max'])
            logging.info('Waiting an additional {} seconds to render target {}'.format(
                offset, self.target))
            sleep(offset)

        logging.info('Rendering template {} for target {}'.format(self.template, self.target))
        try:
            tpl = self.read_template()
        except IOError:
            logging.error('Rendering of {} failed because the file could not be read'.format(
                self.template))
            raise RenderError

        try:
            tpl = self.preprocess(tpl)
        except RetrievalError:
            logging.error('Rendering of {} failed because of an error in preprocessing'.format(
                self.template))
            raise RenderError

        try:
            tpl = self.process(tpl)
        except RetrievalError:
            logging.error('Rendering of {} failed because of an error in processing'.format(
                self.template))
            raise RenderError

        return jinja2.Template(tpl).render()

    def write_file(self, content):
        '''
        Writes `content` to the target file, then sets ownership and mode for the file.

        Arguments:
            content (str): The data to write to the target file
        '''

        try:
            logging.info("Writing {}; setting ownership to {}:{} and mode to {}".format(
                self.target, self.settings['owner'], self.settings['group'], self.settings['chmod']))
            with open(self.target, 'w') as fh:
                fh.write(content)
            shutil.chown(self.target, user=self.settings['owner'], group=self.settings['group'])
            subprocess.run([ 'chmod', self.settings['chmod'], self.target ])
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
            try:
                new_version = self.render()
                try:
                    with open(self.target, 'r') as fh:
                        current_version = fh.read()
                except IOError:
                    logging.error('Cannot read target file {}'.format(self.target))
                    current_version = None

                if new_version != current_version:
                    logging.info('New version of target {} detected'.format(self.target))
                    if not self.write_file(new_version):
                        logging.error('Could not write file {}'.format(self.target))
                        break
                    self.execute_onchange()
                else:
                    logging.info('Target {} is unchanged'.format(self.target))
            except RenderError:
                # At this point, plenty of errors have been emitted; there is nothing else to say.
                # Just move on and wait to try again when the time comes.
                pass

            logging.info('Waiting {} seconds to run target {} again'.format(
                self.settings['frequency'], self.target))
            sleep(self.settings['frequency'])
