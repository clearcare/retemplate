# retemplate
A module to execute a Jinja template on a schedule, supporting multiple backends for value storage.

Currently supported backends:
- Redis
- AWS Secrets Manager Plaintext Secrets
- AWS Systems Manager Parameters

The included `config.yml.example` file provides a sample configuration for Retemplate.

This code almost certainly does not work on non-Unix systems, as it relies on Unix-style permissions
and file ownership to operate. I have no plans to make this work on Windows.

## Installing It
    pip install retemplate

## Running It
Place a `config.yml` file in the cwd, or specify a config file with `-c`.

    rtpl -c /etc/retemplate.yml

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

* Every 60 seconds, retemplate will attempt a two-step parsing of the Jinja template at `/etc/retemplate/hiapi.config.j2`:
  * On the first pass, it will attempt to replace the special "rtpl" URI with the content it refers to.
  * On the second pass, it will render the rest of the Jinja template into memory
* The newly generated template will be compared to the existing one at `/opt/hiapi/config.txt`.
  * If the generated file differs from what is already there, that file will be replaced with the new version. Its ownership and permissions settings will be updated (root:root, 0600). The command `supervisorctl restart hiapi` will be executed.
  * If the two files are the same, retemplate will do nothing.

## Configuration
Retemplate is configured with a YAML file consisting of three main sections:
* `retemplate` (global settings)
* `stores` (data store configuration)
* `templates` (which files get worked over)

### Global Settings
Global settings come under the `retemplate` sections. Currently, the only globally adjustable setting is `logging`, wherein you can supply options to pass into the Python logger library's [basicConfig function](https://docs.python.org/3/library/logging.html#logging.basicConfig). [config.yml.example](config.yml.example) shows a few simple options.

### Data Stores
The `stores` section lets you define your data stores, which are services or functions that retrieve data for templating purposes. There are currently five types of data stores, each with their own configuration options. In the YAML file, these are defined by a dictionary entry where the key is the name of the data store as it will be referenced later in templates and the value is a dictionary of configuration options to be passed into that data store. Although specific configurations may vary between data stores, they all have a `type`, defined below.

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

    rtpl://params/my.system.parameter?WithDecryption=True

#### Local Execution
**type:** *local-exec*

This allows Retemplate to run a command on the server it lives on and use the resulting stdout text as a value. This is useful for when the data you need to produce is more complex than a single stored value. It does run as the same user Retemplate does, so be cautious when using this, since Retemplate often needs to run as root. Registering a local execution data store does require that you define the command it's allowed to run. This is a very basic safety measure, but you don't get any security from this aside from deliberation. **Use with caution.**

    timestamp:
      type: local-exec
      command: date

The path portion of rtpl URIs gets used as a single argument to the command.

    rtpl://timestamp/
