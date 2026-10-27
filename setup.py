__author__ = 'katharine'

import glob
import os

from setuptools import Extension, setup, find_packages

__version__= None  # Overwritten by executing version.py.
with open('pypkjs/version.py') as f:
    exec(f.read())

# Decodes watch microphone audio: libspeex's codec, from the third_party/speex/speex submodule,
# without its tests or the FFT and psychoacoustic code only the preprocessor and VBR use.
SPEEX_UNUSED = ('testenc', 'fftwrap', 'kiss_fft', 'smallft', 'vorbis_psy')
speex = Extension(
    'pypkjs.microphone._speex',
    sources=['pypkjs/microphone/_speex.c'] + sorted(
        path for path in glob.glob('third_party/speex/speex/libspeex/*.c')
        if not os.path.basename(path).startswith(SPEEX_UNUSED)),
    include_dirs=['third_party/speex', 'third_party/speex/speex/include'],
    define_macros=[('HAVE_CONFIG_H', '1')],
)

setup(name='pypkjs',
      version=__version__,
      description='A Pebble phone app simulator written in Python',
      url='https://github.com/coredevices/pypkjs',
      author='Core Devices LLC',
      author_email='griffin@griffinli.com',
      license='MIT',
      packages=find_packages(),
      ext_modules=[speex],
      install_requires=[
          'gevent>=24.11.1',
          'gevent-websocket>=0.10.1',
          'greenlet>=3.2.3',
          'peewee>=3.17.9',
          'pygeoip>=0.3.2',
          'pypng>=0.20220715.0',
          'python-dateutil>=2.4.1',
          'requests>=2.32.3',
          'sh>=2.2.1',
          'six>=1.17.0',
          'websocket-client>=1.8.0',
          'libpebble2>=0.0.27',
          'netaddr>=0.7.18',
          'quickjs-ng>=0.15.1',
      ],
      package_data={
          'pypkjs.javascript.navigator': ['GeoLiteCity.dat'],
          'pypkjs.timeline': ['layouts.json'],
      },
      entry_points={
          'console_scripts': [
            'pypkjs=pypkjs.runner.websocket:run_tool'
          ],
      },
      zip_safe=False)
