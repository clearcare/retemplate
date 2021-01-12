# retemplate
A module to execute a Jinja template on a schedule, supporting multiple backends for value storage.

Currently supported backends:
- AWS Local Metadata Server
- AWS Secrets Manager Plaintext Secrets
- AWS Systems Manager Parameters
- Local Command Execution
- Redis

The included `config.yml.example` file provides a sample configuration for Retemplate.

This code almost certainly does not work on non-Unix systems, as it relies on Unix-style permissions
and file ownership to operate. I have no plans to make this work on Windows.

## Installing It
    pip install retemplate

## Running It
Place a `config.yml` file in the cwd, or specify a config file with `-c`.

    rtpl -c /etc/retemplate.yml

rtpl also supports the `-l` or `--logfile` option, which sets the initial log file for the software.
A very small number of startup messages are logged before the log configuration is processed. On
Python versions prior to 3.8, the `basicConfig` function does not accept the `force` option, which
allows rtpl to reconfigure an existing logger. So if you run Python 3.8 or later, you can just
configure logging in the config file, and you'll only lose a couple mostly insignificant messages.
On older Pythons, all logging will be lost if you don't establish the filename at launch time. Older
Pythons also will not allow custom log formats, etc. **It is strongly recommended that you run
Retemplate on Python 3.8 or later.**

## Use Case
Let's say you have an instance of [PyHiAPI](https://github.com/ryanjjung/pyhiapi) running out of `/opt/hiapi` and it is kept alive by a [supervisord](http://supervisord.org/) configuration. Furthermore, let's say the message returned by HiAPI should match the value stored in a Redis server in a key called `hiapi.message`. So you feed `-c /opt/hiapi/config.txt` to hiapi in your supervisord config so it cares about the content of that file. Then you configure Retemplate to generate that file based on a template. You might create a template at `/etc/retemplate/hiapi.config.j2` that looks like this:

    rtpl://local-redis/hiapi_message

Then create a config file for retemplate that contains these elements:

    stores:
      local-redis:
        type: redis
        host: localhost
        port: 6379
        db: 0
        ssl: False
    templates:
      /opt/hiapi/config.txt:
        template: /etc/retemplate/hiapi.config.j2
        owner: root
        group: root
        chmod: 0600
        frequency: 60
        onchange: supervisorctl restart hiapi

This would lead to the following behavior:

* Every 60 seconds, retemplate will attempt to parse the template at `/etc/retemplate/hiapi.config.j2`.
* The newly generated template will be compared to the existing one at `/opt/hiapi/config.txt`.
  * If the generated file differs from what is already there, that file will be replaced with the new version. Its ownership and permissions settings will be updated (root:root, 0600). The command `supervisorctl restart hiapi` will be executed.
  * If the two files are the same, retemplate will do nothing.

## Configuration
Retemplate is configured with a YAML file consisting of three main sections:
* `retemplate` (global settings)
* `stores` (data store configuration)
* `templates` (which files get worked over)

### Global Settings
Global settings come under the `retemplate` section. Currently, the only globally adjustable setting is `logging`, wherein you can supply options to pass into the Python logger library's [basicConfig function](https://docs.python.org/3/library/logging.html#logging.basicConfig). [config.yml.example](config.yml.example) shows a few simple options.

### Data Stores
The `stores` section defines your data stores, which are services or functions that retrieve data for templating purposes. There are currently five types of data stores, each with their own configuration options. In the configuration file, these are defined by a dictionary entry where the key is the name of the data store as it will be referenced later in templates and the value is a dictionary of configuration options to be passed into that data store. Although specific configurations may vary between data stores, they all have a `type`, defined below.

#### AWS EC2 Instance Metadata Server
**type:** *aws-local-meta*

EC2 instances in the AWS cloud have access to a [metadata server](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instancedata-data-retrieval.html) that reveals a handful of useful values for introspection by processes running on those hosts. Configuration is minimal, requiring only the name and type:

    ec2-metadata:
      type: aws-local-meta

Data is referenced by the URL path used to access it. For example:

    rtpl://ec2-metadata/latest/meta-data/hostname

#### AWS Secrets Manager
**type:** *aws-secrets-manager*

[Secrets Manager](https://aws.amazon.com/secrets-manager/) is an AWS hosted service for managing access to encrypted secrets. This data store is configured by passing in options to the [boto3 client constructor](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html#boto3.session.Session.client).

    secrets:
      type: aws-secrets-manager
      region_name: us-west-2
      aws_access_key_id: ABCDEFG
      aws_secret_access_key: ABCDEFG

Data is referenced by the name of the secret. To retreive the value, the [get_secret_value](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager.html#SecretsManager.Client.get_secret_value) function of boto3 is called, and values referenced in the query portion of the rtpl URI are passed as arguments. For example, you can retrieve a specific secret version like this:

    rtpl://secrets/my.super.duper.secret?VersionId=TheOldOne

#### AWS Systems Manager
**type:** *aws-systems-manager*

[AWS Systems Manager](https://aws.amazon.com/systems-manager/) is a service intended for making configuration information available to various infrastructure resources in an AWS environment. Retemplate supports using its [parameter store](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html) as a data source. Like the Secrets Manager store, this is configured using options to the [boto3 client constructor](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/core/session.html#boto3.session.Session.client).

    params:
      type: aws-systems-manager
      region_name: us-west-2

When referencing values, you can pass parameters to the [get_parameter](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ssm.html#SSM.Client.get_parameter) function in the rtpl query string:

    rtpl://params/my_system_parameter?WithDecryption=True

#### Local Execution
**type:** *local-exec*

This allows Retemplate to run a command on the server it lives on and use the resulting stdout text as a value. This is useful for when the data you need to produce is more complex than a single stored value. It does run as the same user Retemplate does, so be cautious when using this, since Retemplate often needs to run as root. Registering a local execution data store does require that you define the command it's allowed to run. This is a very basic safety measure, but you don't get any security from this aside from deliberation. **Use with caution.**

    timestamp:
      type: local-exec
      command: date

You can supply additional arguments to the command with a series of `arg` variables in the query string. For example, given the above config, the following URI will run the command: `date --rfc-3339 seconds`

    rtpl://timestamp/?arg=--rfc-3339&arg=seconds

#### Redis
**type:** *redis*

Retemplate can talk to a Redis server to retrieve keys from it. This data store assumes Redis is running on the same machine (localhost), on the default port (6379), using the default database ID (0), without SSL, and with no authentication required. To override these settings, configure the data store:

    hosted-redis:
      type: redis
      host: redis.myenvironment.com
      port: 6372
      db: 1
      ssl: True
      auth_token: a_secret_password

The path of the rtpl URI becomes the key to look up in Redis. So if you have previously...

    SET foo bar

...you can retrieve that in an rtpl template with:

    rtpl://hosted-redis/foo

### Templates
Retemplate needs to know what files to template and various other options about each one. Here is a sample config:

    templates:
      /opt/hiapi/config.txt:
        template: /etc/retemplate/hiapi.config.j2
        owner: root
        group: root
        chmod: 0600
        onchange: supervisorctl restart hiapi
        frequency: 60
        random_offset_max: 30

Here, `templates` is the root-level option underneath which all of your templates will be defined. The filename after that (`/opt/hiapi/config.txt`) is the destination for the rendered file. Referencing this config, here is what the rest of the values mean.

* **template**: *The source file, the unrendered template itself.*
* **owner**: *After rendering the template, the file will be owned by this user.*
* **group**: *After rendering the template, the file will be owned by this group.*
* **chmod**: *After rendering the template, this is the octal-format permission for the file.*
* **onchange**: *If the rendered file differs from what was there before, this is a command that will be executed. This should be used for reloading services after their config files have changed.*
* **frequency**: *The number of seconds to wait between template renders.*
* **random_offset_max**: *When present, this causes the rendering process to wait an additional amount of time - up to this number of seconds - before it gets underway. This is designed to prevent the alignment of jobs such that they all make API calls or disk access requests simultaneously. If not set, there is no additional time offset.*

## Writing Templates
The template rendering process is a three-stage one:

1. The template is pre-processed for template variable assignments. These are first looked up, and then references to the variables are substituted. This happens per-line, in a scan-ahead manner, so that variables defined at the top of a file can be used as part of an rtpl:// URI later in the file (see example below).
2. The template is processed to do non-variable lookups of values. These references are then substituted.
3. The template is run through the Jinja2 rendering process.

### Preprocessor Variables
These are contained by curly and angle braces and look like this in a template:

    {< var_name = rtpl://store/key?param=val >}

They are then referenced in the same way, but without the assignment portion:

    {<var_name>}

Referenced variables must be absent of whitespace.

### Example
Let's consider a case where you need to configure an agent with an API key, but that API key varies depending on which environment your system runs in. Further, let's say you have a script on the server that, when run, emits the name of the environment, and that environment name is then used to look up the API key in AWS Secrets Manager.

To accomplish this, you might start with a configuration that looks like this:

    retemplate:
      logging:
        filename: /var/log/retemplate.log
      stores:
        environment:
          type: local-exec
          command: /opt/get-environment.sh
        secrets:
          type: aws-secrets-manager
          region: us-west-2
      templates:
        /etc/agent/config.ini:
          template: /etc/retemplate/agent.config.ini.j2
          owner: agent
          group: agent
          chmod: 0600
          onchange: systemctl restart agent
          frequency: 60
          random_offset_max: 30

You would then write the template that lives at `/etc/retemplate/agent.config.ini.j2` to look something like this:

    {< env = rtpl://environment/ >}
    [agent]
    api_key: rtpl://secrets/{<env>}.agent.api_key

When Retemplate processes this, it goes through the preprocessing stage, storing the output of the `/opt/get-environment.sh` script as the `env` variable. The assignment line is removed from the template entirely. The reference to `{<env>}` on the third line gets replaced with that value. Let's say the value turns out to be `stage`. At this point, when Retemplate goes into its second pass, the template looks like this:

    [agent]
    api_key: rtpl://secrets/stage.agent.api_key

On the second pass, the rtpl URI gets replaced with the value it finds in AWS Secrets Manager. So the template might look like this now:

    [agent]
    api_key: abcd1234

This is then passed to Jinja, which will not detect anything special. So the contents above will be written to `/etc/agent/config.ini`, and the command `systemctl restart agent` will be run if the file produced differs from the file already on disk.
