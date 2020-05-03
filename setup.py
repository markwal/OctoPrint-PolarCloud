# coding=utf-8

########################################################################################################################
plugin_identifier = "polarcloud"
plugin_package = "octoprint_polarcloud"
plugin_name = "OctoPrint-PolarCloud"
plugin_version = "1.9"
plugin_description = """Connects OctoPrint to the PolarCloud so you can easily monitor and control outside of your local network"""
plugin_author = "Mark Walker"
plugin_author_email = "markwal@hotmail.com"
plugin_url = "https://github.com/markwal/OctoPrint-PolarCloud"
plugin_license = "AGPLv3"
plugin_requires = ["SocketIO-client", "pyopenssl"]

### --------------------------------------------------------------------------------------------------------------------
### More advanced options that you usually shouldn't have to touch follow after this point
### --------------------------------------------------------------------------------------------------------------------

# Additional package data to install for this plugin. The subfolders "templates", "static" and "translations" will
# already be installed automatically if they exist. Note that if you add something here you'll also need to update
# MANIFEST.in to match to ensure that python setup.py sdist produces a source distribution that contains all your
# files. This is sadly due to how python's setup.py works, see also http://stackoverflow.com/a/14159430/2028598
plugin_additional_data = []

# Any additional python packages you need to install with your plugin that are not contained in <plugin_package>.*
plugin_additional_packages = []

# Any python packages within <plugin_package>.* you do NOT want to install with your plugin
plugin_ignored_packages = []

# Additional parameters for the call to setuptools.setup. If your plugin wants to register additional entry points,
# define dependency links or other things like that, this is the place to go. Will be merged recursively with the
# default setup parameters as provided by octoprint_setuptools.create_plugin_setup_parameters using
# octoprint.util.dict_merge.
#
# Example:
#     plugin_requires = ["someDependency==dev"]
#     additional_setup_parameters = {"dependency_links": ["https://github.com/someUser/someRepo/archive/master.zip#egg=someDependency-dev"]}
#"dependency_links": ["https://www.dropbox.com/sh/jz5kduz7v6iuqwv/AAB-2vwh0R1_Bkyf_0YSHtz6a?dl=0"]
additional_setup_parameters = {
	}

########################################################################################################################

from setuptools import setup

try:
	import octoprint_setuptools
except:
	print("Could not import OctoPrint's setuptools, are you sure you are running that under "
	      "the same python installation that OctoPrint is installed under?")
	import sys
	sys.exit(-1)

add_pillow = True
try:
	import sys
	import sysconfig
	import distutils.util
	import setuptools

	if int(setuptools.__version__.split('.')[0]) < 40:
		print("May not be able to successfully install with setuptools earlier than 40.0.0. If this fails, upgrade setup tools with 'pip install --upgrade setuptools'.")
	else:
		plat = distutils.util.get_platform().replace('.', '_').replace('-', '_')
		if sys.version_info[0:2] == (2, 7) and plat in ['linux_armv7l', 'linux_armv6l'] and not hasattr(sys, 'pypy_version_info'):
			plugin_requires = [
				"cryptography @ https://markwal.github.io/wheelhouse/cryptography-2.5-cp27-none-" + plat + ".whl",
				"cffi @ https://markwal.github.io/wheelhouse/cffi-1.12.1-cp27-none-" + plat + ".whl",
				"Pillow @ https://markwal.github.io/wheelhouse/Pillow-5.4.1-cp27-none-" + plat + ".whl",
			] + plugin_requires
			add_pillow = False
except:
	import traceback
	traceback.print_exc()
	print("Unable to import Raspberry Pi wheels due to exception, building the slow way.")

if add_pillow:
	plugin_requires = ["Pillow"] + plugin_requires

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
	identifier=plugin_identifier,
	package=plugin_package,
	name=plugin_name,
	version=plugin_version,
	description=plugin_description,
	author=plugin_author,
	mail=plugin_author_email,
	url=plugin_url,
	license=plugin_license,
	requires=plugin_requires,
	additional_packages=plugin_additional_packages,
	ignored_packages=plugin_ignored_packages,
	additional_data=plugin_additional_data
)

if len(additional_setup_parameters):
	from octoprint.util import dict_merge
	setup_parameters = dict_merge(setup_parameters, additional_setup_parameters)

setup(**setup_parameters)
