from setuptools import setup

setup(
    name='retemplate',
    version='0.0.10',
    description="A module to execute a Jinja template on a schedule, supporting several backends for value storage",
    url='https://github.com/ryanjjung/retemplate',
    author='Ryan Jung',
    author_email='ryanjjung@gmail.com',
    license='Apache License 2.0',
    packages=['retemplate'],
    scripts=['rtpl'],
    install_requires=[
        'boto3',
        'jinja2',
        'pyyaml',
        'redis',
        'requests']
    )
