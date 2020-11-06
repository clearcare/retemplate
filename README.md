# retemplate
A module to execute a Jinja template on a schedule, supporting multiple backends for value storage.

Currently supported backends:
- Redis
- AWS Secrets Manager

The included `config.yml.example` file provides a sample configuration for Retemplate.

This code almost certainly does not work on non-Unix systems, as it relies on Unix-style permissions
and file ownership to operate. I have no plans to make this work on Windows.

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
