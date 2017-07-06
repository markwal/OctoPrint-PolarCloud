# coding=utf-8

from __future__ import absolute_import

__author__ = "Mark Walker (markwal@hotmail.com)"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2017 Mark Walker"

"""
    This file is part of OctoPrint-PolarCloud.

    OctoPrint-PolarCloud is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    OctoPrint-PolarCloud is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with OctoPrint-PolarCloud.  If not, see <http://www.gnu.org/licenses/>.
"""

import os
import sys
import stat
import threading
import logging
import uuid
import Queue
import base64
import datetime
from time import sleep
from StringIO import StringIO
import io
from urlparse import urlparse, urlunparse
import random
import re

from OpenSSL import crypto
from socketIO_client import SocketIO, LoggingNamespace, TimeoutError, ConnectionError
import sarge
import flask
from flask.ext.babel import gettext, _
import requests
from PIL import Image

import octoprint.plugin
import octoprint.util
from octoprint.util import get_exception_string
from octoprint.events import Events
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.util import StreamWrapper
from octoprint.settings import settings

logging.getLogger('socketIO-client').setLevel(logging.DEBUG)
# logging.basicConfig()

# what's a mac address we can use as an identifier?
def get_mac():
	return ':'.join(('%012X' % uuid.getnode())[i:i+2] for i in range(0, 12, 2))

# what's the likely ip address for the local UI?
def get_ip():
	return octoprint.util.address_for_client("google.com", 80)

# take a server relative or localhost url and attempt to make absolute an absolute
# url out of it  (guess about which interface)
def normalize_url(url):
	urlp = urlparse(url)
	scheme = urlp.scheme
	if not scheme:
		scheme = "http"
	host = urlp.netloc
	if not host or host == '127.0.0.1' or host == 'localhost':
		host = get_ip()
	return urlunparse((scheme, host, urlp.path, urlp.params, urlp.query, urlp.fragment))

# do a dictionary lookup and return an empty string for any missing key
# rather than throw MissingKey
def str_safe_get(dictionary, *keys):
	return reduce(lambda d, k: d.get(k) if isinstance(d, dict) else "", keys, dictionary)
def float_safe_get(dictionary, *keys):
	s = str_safe_get(dictionary, *keys)
	return 0.0 if not s else float(s)

# return true if each of the list of keys are in the dictionary, otherwise false
def has_all(dictionary, *keys):
	for key in keys:
		if not key in dictionary:
			return False
	return True


class PolarcloudPlugin(octoprint.plugin.SettingsPlugin,
                       octoprint.plugin.AssetPlugin,
                       octoprint.plugin.TemplatePlugin,
                       octoprint.plugin.StartupPlugin,
                       octoprint.plugin.SimpleApiPlugin,
                       octoprint.plugin.EventHandlerPlugin):
	PSTATE_IDLE = "0"
	PSTATE_SERIAL = "1"         # Printing a local print over serial
	PSTATE_PREPARING = "2"      # Preparing a cloud print (slicing)
	PSTATE_PRINTING = "3"       # Printing a cloud print
	PSTATE_PAUSED = "4"
	PSTATE_POSTPROCESSING = "5" # Performing post-print operations
	PSTATE_CANCELLING = "6"     # Canceling a print originated from the cloud
	PSTATE_COMPLETE = "7"       # Completed a print originated from the cloud
	PSTATE_UPDATING = "8"       # Busy updating OctoPrint and/or plugins
	PSTATE_COLDPAUSED = "9"
	PSTATE_CHANGINGFILAMENT = "10"
	PSTATE_TCPIP = "11"         # Printing a local print over TCP/IP
	PSTATE_ERROR = "12"
	PSTATE_OFFLINE = "13"

	def __init__(self):
		self._serial = None
		self._socket = None
		self._connected = False
		self._status_now = False
		self._challenge = None
		self._task_queue = Queue.Queue()
		self._polar_status_worker = None
		self._upload_location = {}
		self._update_interval = 60
		self._cloud_print = False
		self._cloud_print_info = {}
		self._job_pending = False
		self._job_id = "123"
		self._pstate = self.PSTATE_IDLE # only applies if _cloud_print
		self._pstate_counter = 0
		self._max_image_size = 150000
		self._image_transpose = False
		self._printer_type = None
		self._disconnect_on_register = False
		self._hello_sent = False

		# consider temp reads higher than this as having a target set for more
		# frequent reports
		self._set_temp_threshold = 50

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self, *args, **kwargs):
		return dict(
			service="https://printer2.polar3d.com",
			service_ui="https://polar3d.com",
			serial=None,
			printer_type=None,
			email="",
			max_image_size = 150000,
			verbose=False,
			upload_timelapse=True,
			enable_system_commands=True
		)

	def _update_local_settings(self):
		self._logger.setLevel(logging.DEBUG if self._settings.get(['verbose']) else logging.NOTSET)
		self._logger.debug("_update_local_settings")
		self._max_image_size = self._settings.get(['max_image_size'])
		self._serial = self._settings.get(['serial'])
		self._image_transpose = (self._settings.global_get(["webcam", "flipH"]) or
				self._settings.global_get(["webcam", "flipV"]) or
				self._settings.global_get(["webcam", "rotate90"]))

		self._snapshot_url = self._settings.global_get(["webcam", "snapshot"])
		if self._snapshot_url:
			self._snapshot_url = self._snapshot_url

	##~~ AssetPlugin mixin

	def get_assets(self, *args, **kwargs):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/polarcloud.js"],
			css=["css/polarcloud.css"],
			less=["less/polarcloud.less"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self, *args, **kwargs):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		return dict(
			polarcloud=dict(
				displayName="Polarcloud Plugin",
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="markwal",
				repo="OctoPrint-PolarCloud",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/markwal/OctoPrint-PolarCloud/archive/{target_version}.zip"
			)
		)

	##~~ StartupPlugin mixin

	def on_after_startup(self, *args, **kwargs):
		if self._settings.get(['verbose']):
			self._logger.setLevel(logging.DEBUG)
		self._logger.debug("on_after_startup")
		self._get_keys()
		self._update_local_settings()
		if self._serial:
			self._start_polar_status()

	##~~ utility functions

	def _get_job_id(self):
		if self._printer.is_printing() or self._printer.is_paused():
			return self._job_id
		else:
			return '0'

	def _valid_packet(self, data):
		if not self._serial or self._serial != data.get("serialNumber", ""):
			self._logger.debug("Serial number is '{}'".format(repr(self._serial)))
			self._logger.debug("Ignoring message (mismatch serial): {}".format(repr(data)))
			return False
		return True

	##~~ polar communication

	def _create_socket(self):
		self._logger.debug("_create_socket")

		# Create socket and set up event handlers
		try:
			self._challenge = None
			self._connected = True
			self._hello_sent = False
			self._socket = SocketIO(self._settings.get(['service']), Namespace=LoggingNamespace, verify=True, wait_for_connection=False)
		except (TimeoutError, ConnectionError, StopIteration):
			self._socket = None
			self._logger.exception('Unable to open socket {}'.format(get_exception_string()))
			return

		# Register all the socket messages
		self._socket.on('disconnect', self._on_disconnect)
		self._socket.on('registerResponse', self._on_register_response)
		self._socket.on('welcome', self._on_welcome)
		self._socket.on('getUrlResponse', self._on_get_url_response)
		self._socket.on('cancel', self._on_cancel)
		self._socket.on('command', self._on_command)
		self._socket.on('pause', self._on_pause)
		self._socket.on('print', self._on_print)
		self._socket.on('resume', self._on_resume)
		self._socket.on('temperature', self._on_temperature)
		self._socket.on('update', self._on_update)
		self._socket.on('connectPrinter', self._on_connect_printer)
		self._socket.on('customCommand', self._on_custom_command)

	def _start_polar_status(self):
		if not self._polar_status_worker:
			self._logger.debug("starting heartbeat")
			self._polar_status_worker = threading.Thread(target=self._polar_status_heartbeat)
			self._polar_status_worker.daemon = True
			self._polar_status_worker.start()

	def _system(self, command_line):
		try:
			p = sarge.run(command_line, stderr=sarge.Capture())
			return (p.returncode, p.stderr.text)
		except:
			self._logger.exception("Failed to run system command: {}".format(command_line))
			return (1, "")


	def _get_keys(self):
		data_folder = self.get_plugin_data_folder()
		key_filename = os.path.join(data_folder, 'p3d_key')
		self._logger.debug('key_filename: {}'.format(key_filename))
		if not os.path.isfile(key_filename):
			self._logger.debug('Generating key pair')
			key = crypto.PKey()
			key.generate_key(crypto.TYPE_RSA, 2048)
			with open(key_filename, 'w') as f:
				f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, key))
			if sys.platform != 'win32':
				os.chmod(key_filename, stat.S_IRUSR | stat.S_IWUSR)
		try:
			with open(key_filename) as f:
				key = f.read()
			self._key = crypto.load_privatekey(crypto.FILETYPE_PEM, key)
		except:
			self._key = None
			self._logger.error("Unable to generate or access key.")
			return

		if hasattr(self._key, 'dump_publickey'):
			self._public_key = crypto.dump_publickey(crypto.FILETYPE_PEM, self._key)
		else:
			pubkey_filename = key_filename + ".pub"
			if not os.path.isfile(pubkey_filename) or os.path.getsize(pubkey_filename) == 0:
				if sys.platform != 'win32':
					os.chmod(key_filename, stat.S_IRUSR | stat.S_IWUSR)
				command_line = "ssh-keygen -e -m PEM -f {key_filename} > {pubkey_filename}".format(key_filename=key_filename, pubkey_filename=pubkey_filename)
				returncode, stderr_text = self._system(command_line)
				if returncode != 0:
					self._logger.error("Unable to generate public key (may need to manually upgrade pyOpenSSL, see README) {}: {}".format(returncode, stderr_text))
					self._key = None
					try:
						os.remove(pubkey_filename)
					except OSError:
						pass
					return
			with open(pubkey_filename) as f:
				self._public_key = f.read()

	def _polar_status_from_state(self):
		state_mapping = {
			"OPEN_SERIAL": self.PSTATE_ERROR,
			"DETECT_SERIAL": self.PSTATE_ERROR,
			"DETECT_BAUDRATE": self.PSTATE_ERROR,
			"CONNECTING": self.PSTATE_ERROR,
			"OPERATIONAL": self.PSTATE_IDLE,
			"PRINTING": self.PSTATE_SERIAL,
			"PAUSED": self.PSTATE_PAUSED,
			"CLOSED": self.PSTATE_ERROR,
			"ERROR": self.PSTATE_ERROR,
			"CLOSED_WITH_ERROR": self.PSTATE_ERROR,
			"TRANSFERING_FILE": self.PSTATE_SERIAL,
			"OFFLINE": self.PSTATE_OFFLINE,
			"UNKNOWN": self.PSTATE_ERROR,
			"NONE": self.PSTATE_ERROR
		}
		# this is a bit complicated because the mapping isn't direct and while
		# we try to keep track of current polar state, current octoprint state
		# wins, so we let _pstate show through if it "matches" current octoprint
		if self._cloud_print:
			if self._pstate_counter:
				# if we've got a counter, we're still repeating completion/cancel
				# message, do that
				self._pstate_counter -= 1
				if not self._pstate_counter:
					self._cloud_print = False
				return self._pstate
			if self._pstate == self.PSTATE_POSTPROCESSING:
				return self._pstate

		self._logger.debug("OctoPrint state: {}".format(self._printer.get_state_id()))
		state = state_mapping[self._printer.get_state_id()]

		if state == self.PSTATE_SERIAL:
			# if we were ever printing, we owe a "job" completion message
			self._job_pending = True
		if self._cloud_print:
			if state == self.PSTATE_IDLE and self._pstate == self.PSTATE_PREPARING:
				# octoprint thinks were idle, but we must be slicing
				return self._pstate
			if state == self.PSTATE_SERIAL:
				# octoprint thinks we're printing
				return self.PSTATE_PRINTING
			if state != self.PSTATE_PAUSED:
				# if we aren't preparing, printing or paused and we're not
				# counting down anymore, we must really be done
				self._cloud_print = False
				self._job_id = "123"
				self._cloud_print_info = {}
		return state

	def _current_status(self):
		temps = self._printer.get_current_temperatures()
		self._logger.debug("temps: {}".format(repr(temps)))
		status = {
			"serialNumber": self._serial,
			"status": self._polar_status_from_state(),
			"jobId": self._get_job_id(),
			"protocol": "2",
			"progress": "",
			"progressDetail": "",
			"estimatedTime": "0",
			"filamentUsed": "0",
			"startTime": "0",
			"printSeconds": "0",
			"bytesRead": "0",
			"fileSize": "0",
			"file": "",          # url for cloud stl
			"config": "",        # url for cloud config.ini
			"sliceDetails": "",  # Cura_SteamEngine output
			"securityCode": ""   # three colors
		}
		target_set = False
		if 'tool0' in temps:
			status['tool0'] = temps['tool0']['actual']
			status['targetTool0'] = temps['tool0']['target']
			if status['targetTool0'] > 0 or status['tool0'] > self._set_temp_threshold:
				target_set = True
		if 'tool1' in temps:
			status['tool1'] = temps['tool1']['actual']
			status['targetTool1'] = temps['tool1']['target']
			if status['targetTool1'] > 0 or status['tool1'] > self._set_temp_threshold:
				target_set = True
		if 'bed' in temps:
			status['bed'] = temps['bed']['actual']
			status['targetBed'] = temps['bed']['target']
			if status['targetBed'] > 0 or status['bed'] > self._set_temp_threshold:
				target_set = True

		if self._printer.is_printing() or self._printer.is_paused():
			data = self._printer.get_current_data()
			self._logger.debug("get_current_data() is {}".format(repr(data)))
			status["progress"] = str_safe_get(data, 'state', 'text')
			status["progressDetail"] = "Printing Job: {} Percent Complete: {:0.1f}%".format(
				str_safe_get(data, 'file', 'name'), float_safe_get(data, 'progress', 'completion'))
			status["estimatedTime"] = str_safe_get(data, "job", "estimatedPrintTime")
			status["filamentUsed"] = str_safe_get(data, "job", "filament", "length")
			status["printSeconds"] = str_safe_get(data, "progress", "printTime")
			if status["printSeconds"]:
				status["startTime"] = (datetime.datetime.now() -
						datetime.timedelta(seconds=int(status["printSeconds"]))).isoformat()
			status["bytesRead"] = str_safe_get(data, "progress", "filepos")
			status["fileSize"] = str_safe_get(data, "job", "file", "size")

		return status, target_set

	# thread to update the polar cloud with current status periodically
	def _polar_status_heartbeat(self):

		def _wait_and_process(seconds, ignore_status_now=False):
			try:
				for i in range(seconds):
					if not self._task_queue.empty():
						try:
							task = self._task_queue.get_nowait()
							task()
						except Queue.Empty:
							pass
					if not ignore_status_now and self._status_now:
						self._status_now = False
						self._logger.debug("_status_now break")
						return False
					self._socket.wait(seconds=1)
					if not self._connected:
						self._socket = None
						return False
				return True
			except:
				if not self._connected:
					# likely throw from disconnect
					self._socket = None
				else:
					self._logger.exception("polar_heartbeat exception")
					sleep(5)
				return False

		try:
			self._logger.debug("heartbeat")
			random.seed()
			next_check_versions = datetime.datetime.now() 
			status_sent = 0
			self._create_socket()

			while True:
				self._logger.debug("self._socket: {}".format(repr(self._socket)))
				if self._socket:
					_wait_and_process(10)
				else:
					reconnection_delay = random.uniform(1.5, 3)
					self._logger.warn("unable to create socket to Polar Cloud, check again in {} seconds".format(reconnection_delay))
					sleep(reconnection_delay)
					self._create_socket()

				# wait until we get a hello
				if not self._hello_sent:
					continue

				self._status_now = False
				_wait_and_process(5, True)
				if self._socket:
					self._ensure_upload_url('idle')
					if self._settings.get_boolean(['enable_system_commands']):
						self._custom_command_list()
				skip_snapshot = False

				while self._connected:
					status, target_set = self._current_status()
					self._logger.debug("emit status: {}".format(repr(status)))
					self._socket.emit("status", status)
					status_sent += 1

					if datetime.datetime.now() > next_check_versions:
						self._check_versions()
						next_check_versions = datetime.datetime.now() + datetime.timedelta(days=1)

					# reset update interval to slow if we're not printing anymore
					# we do it here so we get one quick update when it changes
					if target_set:
						self._update_interval = 10
					elif not self._cloud_print and not self._printer.is_printing():
						self._update_interval = 60

					if _wait_and_process(self._update_interval):
						if self._printer.is_closed_or_error() and not self._printer.is_error():
							if skip_snapshot:
								continue
							skip_snapshot = True
						else:
							skip_snapshot = False
						self._upload_snapshot()

				self._logger.info("Socket disconnected, clear and restart")
				if status_sent < 3 and not self._disconnect_on_register:
					self._logger.warn("Unable to connect to Polar Cloud")
					break
				self._logger.debug("bottom of forever")

		except:
			self._logger.exception("heartbeat failure")
			return

	def _on_disconnect(self):
		self._logger.debug("[Disconnected]")
		self._connected = False

	#~~ time-lapse and snapshots to cloud

	def _create_timelapse(self):
		# TODO figure out how to timebox/sizebox the timelapse
		'gst-launch-1.0 qtmux name=mux ! filesink location="$ARG2"  multifilesrc location="$ARG1" index=1 caps="image/jpeg,framerate=\(fraction\)12/1" ! jpegdec ! videoconvert ! videorate ! x264enc ! mux .'

	def _ensure_upload_url(self, upload_type):
		if not self._snapshot_url:
			return False
		if upload_type != 'idle' and upload_type in self._upload_location and self._upload_location[upload_type]['jobID'] != self._job_id:
			self._logger.debug("Discarding old upload url: {} for {}".format(upload_type, self._upload_location[upload_type]['jobID']))
			del self._upload_location[upload_type]
		if not upload_type in self._upload_location or datetime.datetime.now() > self._upload_location[upload_type]['expires']:
			self._get_url(upload_type, self._get_job_id() if upload_type == 'idle' else self._job_id)
			return False
		return True

	def _upload_snapshot(self):
		self._logger.debug("_upload_snapshot")
		upload_type = 'idle'
		if self._cloud_print and self._job_id != '123' and (self._printer.is_printing() or self._printer.is_paused()):
			upload_type = 'printing'
		self._logger.debug("upload_type {}".format(upload_type))
		if not self._ensure_upload_url(upload_type):
			return
		try:
			loc = self._upload_location[upload_type]
			r = requests.get(self._snapshot_url, timeout=5)
			r.raise_for_status()
		except Exception:
			self._logger.exception("Could not capture image from {}".format(self._snapshot_url))
			return

		try:
			image_bytes = r.content
			image_size = len(image_bytes)
			if self._image_transpose or image_size > self._max_image_size:
				self._logger.debug("Recompressing snapshot to smaller size")
				buf = StringIO()
				buf.write(image_bytes)
				image = Image.open(buf)
				image.thumbnail((640, 480))
				if self._settings.global_get(["webcam", "flipH"]):
					image = image.transpose(Image.FLIP_LEFT_RIGHT)
				if self._settings.global_get(["webcam", "flipV"]):
					image = image.transpose(Image.FLIP_TOP_BOTTOM)
				if self._settings.global_get(["webcam", "rotate90"]):
					image = image.transpose(Image.ROTATE_90)
				image_bytes = StringIO()
				image.save(image_bytes, format="jpeg")
				image_bytes.seek(0, 2)
				new_image_size = image_bytes.tell()
				image_bytes.seek(0)
				self._logger.debug("Image transcoded from size {} to {}".format(image_size, new_image_size))
				image_size = new_image_size
			if image_size == 0:
				self._logger.debug("Image content is length 0 from {}, not uploading to PolarCloud".format(self._snapshot_url))
				return
			p = requests.post(loc['url'], data=loc['fields'], files={'file': ('image.jpg', image_bytes)})
			p.raise_for_status()
			self._logger.debug("{}: {}".format(p.status_code, p.content))

			self._logger.debug("Image captured from {}".format(self._snapshot_url))
		except Exception:
			self._logger.exception("Could not post snapshot to PolarCloud")

	def _upload_timelapse(self, path):
		self._logger.debug("_upload_timelapse")
		self._pstate = self.PSTATE_COMPLETE
		self._pstate_counter = 3
		if not self._ensure_upload_url('timelapse'):
			self._logger.error("Unable to retrieve valid destination to upload timelapse {}".format(path))
			return
		try:
			self._logger.debug("Uploading timelapse {}".format(path))
			loc = self._upload_location['timelapse']
			p = requests.post(loc['url'], data=loc['fields'], files={'file': ('timelapse.mp4', open(path, 'rb'))})
			p.raise_for_status()
			self._logger.debug("timelapse upload result {}: {}".format(p.status_code, p.content))
		except Exception:
			self._logger.exception("Could not upload timelapse {} to PolarCloud".format(path))

	#~~ getUrl -> polar: getUrlResponse

	def _on_get_url_response(self, response, *args, **kwargs):
		self._logger.debug('getUrlResponse {}'.format(repr(response)))
		if not self._valid_packet(response):
			return
		if not has_all(response, 'status'):
			self._logger.warn('getUrlResponse lacks status property')
			return
		if not response['status'] == 'SUCCESS':
			self._logger.warn('Failed to get upload url: {} {}'
				.format(response['status'], response.get('message', '')))
			return
		if not has_all(response, 'type', 'expires', 'url', 'maxSize', 'fields'):
			self._logger.warn('getUrlResponse lacks a required property')
		response["expires"] = (datetime.datetime.now() + datetime.timedelta(seconds=int(response.get("expires", 0))))
		if not has_all(response, 'jobID'):
			response["jobID"] = self._job_id
		self._upload_location[response.get('type', 'idle')] = response
		self._logger.debug('response_type = {}'.format(response.get('type', '')))
		if response.get('type', '') == 'idle':
			self._task_queue.put(self._upload_snapshot)

	# get upload url from the cloud
	# url_type - 'idle' | 'printing' | 'timelapse'
	#	'printing'/'timelapse' for cloud initiated print only
	# job_id - cloud assigned print job id ('123' for local print)
	def _get_url(self, url_type, job_id):
		self._logger.debug('getUrl url_type: {}, job_id: {}'.format(url_type, job_id))
		self._socket.emit('getUrl', {
			'serialNumber': self._serial,
			'method': 'post',
			'type': url_type,
			'jobId': job_id
		})

	#~~ polar: welcome -> hello

	def _on_welcome(self, welcome, *args, **kwargs):
		self._logger.debug('_on_welcome: {}'.format(repr(welcome)))
		if 'challenge' in welcome:
			self._challenge = welcome['challenge']
			if isinstance(self._challenge, unicode):
				self._challenge = self._challenge.encode('utf-8')
			self._task_queue.put(self._hello)
			self._start_polar_status()

	def _hello(self):
		self._logger.debug('hello')
		if self._serial and self._challenge:
			self._hello_sent = True
			self._status_now = True
			self._logger.debug('emit hello')
			self._printer_type = self._settings.get(["printer_type"])
			camUrl = self._settings.global_get(["webcam", "stream"])
			try:
				if camUrl:
					camUrl = normalize_url(camUrl)
			except:
				self._logger.exception("Unable to canonicalize the url {}".format(camUrl))
			self._logger.debug("camUrl: {}".format(camUrl))
			self._socket.emit('hello', {
				'serialNumber': self._serial,
				'signature': base64.b64encode(crypto.sign(self._key, self._challenge, b'sha256')),
				'MAC': get_mac(),
				'localIP': get_ip(),
				'protocol': '2',
				'camUrl': camUrl,
				'rotateImg': 1 if self._settings.global_get(["webcam", "flipV"]) else 0,
				'printerType': self._printer_type
			})
			self._challenge = None
		else:
			self._logger.debug('skip emit hello, serial: {}'.format(self._serial))

	#~~ register -> polar: registerReponse

	def _on_register_response(self, response, *args, **kwargs):
		self._logger.debug('on_register_response: {}'.format(repr(response)))
		if 'serialNumber' in response:
			self._serial = response['serialNumber']
			self._settings.set(['serial'], self._serial)
			self._status_now = True
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'serial',
				'serial': self._serial
			})
			self._disconnect_on_register = True
			self._socket.disconnect()
		else:
			reason = ""
			if 'reason' in response:
				if response['reason'] in ['MFG_MISSING', 'MFG_UNKNOWN']:
					reason = _("There is a problem or a bug in this plugin.")
				elif response['reason'] == 'EMAIL_PIN_ERROR':
					reason = _("The e-mail address and/or the PIN are not recognized by Polar Cloud.")
				elif response['reason'] == 'SERVER_ERROR':
					reason = _("Polar Cloud was unable to add the printer. Try again later.")
				elif response['reason'] == 'FORBIDDEN':
					reason = _("This OctoPrint instance is already registered to another account.")
			# WARNING do not send unencoded user input in 'reason' since it is
			# rendered directly into the HTML of the page
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'registration_failed',
				'reason': reason
			})

	def _register(self, email, pin):
		self._get_keys()
		if not self._key:
			self._logger.info("Can't register because unable to generate signing key")
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'registration_failed',
				'reason': _('The plugin failed to generate a signing key. Please see troubleshooting tips in the <A href="https://github.com/markwal/OctoPrint-PolarCloud/blob/master/README.md">README</A>.')
			})
			return False

		if not self._socket:
			self._start_polar_status()
			sleep(2) # give the thread a moment to start communicating
			self._logger.debug("Do we have a socket: {}".format(repr(self._socket)))
		if not self._socket:
			self._logger.info("Can't register because unable to communicate with Polar Cloud")
			return False

		self._logger.info("emit register")
		self._socket.emit("register", {
			"mfg": "op",
			"email": email,
			"pin": pin,
			"publicKey": self._public_key,
			"myInfo": {
				"MAC": get_mac(),
				"protocolVersion": "2"
			}
		})
		return True

	#~~ cancel

	def _on_cancel(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.cancel_print()
		self._status_now = True

	#~~ command

	def _on_command(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.commands(data.get("command", ""))
		self._status_now = True
		# TODO commandResponse?

	#~~ pause

	def _on_pause(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		# TODO data['type'] = filament, cold, pause
		self._printer.pause_print()
		self._status_now = True

	#~~ print

	def _on_print(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		if self._printer.is_printing() or self._printer.is_paused():
			self._logger.warn("PolarCloud sent print command, but OctoPrint is already printing.")
			return

		self._job_id = "123"
		if not 'stlFile' in data:
			self._logger.warn("PolarCloud sent print command without stl file path.")
			return

		info = {}
		gcode = (".gcode" in data['stlFile'].lower())
		pos = (0, 0)
		if not gcode:
			if not 'configFile' in data:
				self._logger.warn("PolarCloud sent print command without slicing profile.")
				return
			info['config'] = data['configFile']
			try:
				req_ini = requests.get(data['configFile'], timeout=5)
				req_ini.raise_for_status()
			except Exception:
				self._logger.exception("Could not retrieve slicer config file from PolarCloud: {}".format(data['configFile']))
				return
			(slicing_profile, pos) = self._create_slicing_profile(req_ini.content)
			if not slicing_profile:
				self._logger.warn("Unable to create slicing profile. Aborting slice and print.")
				return

		# TODO: use tornado async I/O to get the print file?
		try:
			info['file'] = data['stlFile']
			req_stl = requests.get(data['stlFile'], timeout=5)
			req_stl.raise_for_status()
		except Exception:
			self._logger.exception("Could not retrieve print file from PolarCloud: {}".format(data['stlFile']))
			return

		path = self._file_manager.add_folder(FileDestinations.LOCAL, "polarcloud")
		path = self._file_manager.join_path(FileDestinations.LOCAL, path, "current-print")
		pathGcode = path + ".gcode"
		path = path + (".gcode" if gcode else ".stl")
		self._file_manager.add_file(FileDestinations.LOCAL, path, StreamWrapper(path, io.BytesIO(req_stl.content)), allow_overwrite=True)
		job_id = data['jobId'] if 'jobId' in data else "123"
		self._logger.debug("print jobId is {}".format(job_id))
		self._logger.debug("print data is {}".format(repr(data)))

		if self._printer.is_closed_or_error():
			self._printer.disconnect()
			self._printer.connect()

		self._cloud_print = True
		self._job_pending = True
		self._job_id = job_id
		self._pstate_counter = 0
		self._pstate = self.PSTATE_PREPARING
		self._cloud_print_info = info
		self._status_now = True

		if not gcode:
			try:
				self._file_manager.slice('cura',
						FileDestinations.LOCAL, path,
						FileDestinations.LOCAL, pathGcode,
						position=pos, profile="polarcloud",
						callback=self._on_slicing_complete,
						callback_args=(self._file_manager.path_on_disk(FileDestinations.LOCAL, pathGcode),))
			except:
				self._logger.exception("Unable to slice.")
				self._pstate = self.PSTATE_ERROR
				self._pstate_counter = 3
		else:
			self._on_slicing_complete(path)

	def _on_slicing_complete(self, path, *args, **kwargs):
		# TODO store self._cloud_print_info[sliceDetails]
		self._logger.debug("_on_slicing_complete")
		self._pstate = self.PSTATE_PRINTING
		self._printer.select_file(path, False, printAfterSelect=True)
		self._update_interval = 10
		self._status_now = True

	#~~ resume

	def _on_resume(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._printer.resume_print()
		self._status_now = True

	#~~ temperature

	def _on_temperature(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		for key in data:
			if re.match("(?:bed)|(?:tool[0-9]+)", key):
				self._logger.debug("set_temperature {} to {}", key, data[key])
				self._printer.set_temperature(key, data[key])
		self._status_now = True

	#~~ update

	def _on_update(self, data, *args, **kwargs):
		if not self._valid_packet(data):
			return
		self._logger.debug("update")
		try:
			softwareupdate = self._get_softwareupdate_plugin()
			if softwareupdate:
				softwareupdate.perform_updates()
		except:
			self._logger.exception("Couldn't perform update via softwareupdate plugin")

	def _get_softwareupdate_plugin(self):
		softwareupdate = self._plugin_manager.get_plugin_info('softwareupdate')
		if softwareupdate and 'implementation' in dir(softwareupdate):
			return softwareupdate.implementation
		return None

	#~~ customCommandList -> polar: customCommand

	def _custom_command_list(self):
		def _polar_custom_from_command(source, command):
			custom = {
				"label": str_safe_get(command, "name"),
				"command": source + "/" + str_safe_get(command, "action")
			}
			confirm = str_safe_get(command, "confirm")
			if confirm:
				custom["confirmText"] = confirm
			return custom

		self._logger.debug("generating customCommandList")
		command_list = []
		try:
			from octoprint.server.api.system import _get_core_command_specs as system_commands
			for command in system_commands().values():
				command_list.append(_polar_custom_from_command("core", command))
		except Exception:
			self._logger.exception("Could not retrieve system commands")

		for command in self._settings.global_get(["system", "actions"]):
			if not "action" in command:
				continue
			command_list.append(_polar_custom_from_command("custom", command))

		self._logger.debug("customCommandList")
		self._socket.emit('customCommandList', {
			'serialNumber': self._serial,
			'customCommandList': command_list
		})

	def _on_custom_command(self, data, *args, **kwargs):
		self._logger.debug("customCommand: {}".format(repr(data)))
		try:
			source, command = data.split("/", 1)
			from octoprint.server.api.system import executeSystemCommand
			executeSystemCommand(source, command)
		except Exception:
			self._logger.exception("Could not execute system command: {}".format(repr(data)))

	#~~ setVersion

	def _check_versions(self):
		running_version = 'unknown'
		latest_version = 'unknown'
		try:
			softwareupdate = self._get_softwareupdate_plugin()
			if softwareupdate:
				version_info = softwareupdate.get_current_versions(['octoprint'])[0]['octoprint']
				self._logger.debug("version_info: {}".format(repr(version_info)))
				running_version = version_info['information']['local']['value']
				latest_version = version_info['information']['remote']['value']
		except:
			self._logger.exception("Couldn't get softwareupdate plugin information")
			return

		if running_version == 'unknown' or latest_version == 'unknown':
			self._logger.warn("Unable to determine current version or available version of OctoPrint")
			return

		self._logger.debug('setVersion')
		self._socket.emit('setVersion', {
			'serialNumber': self._serial,
			'runningVersion': running_version,
			'latestVersion': latest_version
		})

	#~~ job

	def _job(self, job_id, state):
		self._logger.debug('job')
		self._job_pending = False
		if self._serial:
			self._socket.emit('job', {
				'serialNumber': self._serial,
				'jobId': job_id,
				'state': state
			})
		self._status_now = True

	#~~ connectPrinter

	def _on_connect_printer(self, data, *args, **kwargs):
		self._logger.debug("connectPrinter")
		if self._printer.is_closed_or_error():
			self._logger.info("Attempting to reconnect to the printer")
			try:
				self._printer.disconnect()
				self._printer.connect()
			except:
				self._logger.exception("Unable to reconnect to the printer")
		self._status_now = True

	#~~ EventHandlerPlugin mixin

	def on_event(self, event, payload):
		self._logger.debug("on_event: {}".format(repr(event)))
		if event == Events.PRINT_CANCELLED or event == Events.PRINT_FAILED:
			self._pstate = self.PSTATE_CANCELLING
			if self._cloud_print:
				self._pstate_counter = 3
		elif event == Events.PRINT_STARTED or event == Events.PRINT_RESUMED:
			self._pstate = self.PSTATE_PRINTING
			self._update_interval = 10
			self._logger.debug("Update interval to {}".format(self._update_interval))
		elif event == Events.ERROR:
			self._pstate = self.PSTATE_ERROR
		elif event == Events.PRINT_PAUSED:
			self._pstate = self.PSTATE_PAUSED
		elif event == Events.PRINT_DONE:
			self._pstate = self.PSTATE_COMPLETE
			if self._cloud_print:
				self._pstate = self.PSTATE_POSTPROCESSING
				self._pstate_counter = 3
			self._job(self._job_id, "completed")
		elif event == Events.SLICING_CANCELLED or event == Events.SLICING_FAILED:
			self._pstate = self.PSTATE_CANCELLING
			self._pstate_counter = 3
		elif event == Events.SETTINGS_UPDATED:
			self._update_local_settings()
			if (self._printer_type != self._settings.get(['printer_type'])):
				self._task_queue.put(self._hello)
			self._status_now = True
			return
		elif event == Events.MOVIE_RENDERING or event == Events.POSTROLL_START:
			if self._cloud_print:
				self._pstate = self.PSTATE_POSTPROCESSING
				self._pstate_counter = 0
			self._status_now = True
			return
		elif event == Events.MOVIE_FAILED:
			self._pstate = self.PSTATE_IDLE
			if self._cloud_print:
				self._pstate = self.PSTATE_COMPLETE
				self._pstate_counter = 3
			self._status_now = True
			return
		elif event == Events.MOVIE_DONE:
			if self._cloud_print and self._settings.get_boolean(['upload_timelapse']):
				self._ensure_upload_url('timelapse')
				translate = PolarTimelapseTranscoder(payload["movie"],
						self._upload_timelapse, self._logger)
				self._pstate = self.PSTATE_POSTPROCESSING
				translate.translate_timelapse()
			else:
				self._pstate = self.PSTATE_COMPLETE
				self._pstate_counter = 3
		elif hasattr(Events, 'PRINTER_STATE_CHANGED') and event == Events.PRINTER_STATE_CHANGED:
			self._status_now = True
			return
		else:
			return

		self._status_now = True
		if self._job_pending and not self._printer.is_printing() and not self._printer.is_paused() and self._pstate != self.PSTATE_PREPARING:
			self._logger.debug("emitting job due to event: {}".format(event))
			self._job(self._job_id, "canceled")

	#~~ SimpleApiPlugin mixin

	def get_api_commands(self, *args, **kwargs):
		return dict(
			register=[]
		)

	def is_api_adminonly(self, *args, **kwargs):
		return True

	def on_api_command(self, command, data):
		status='FAIL'
		message=''
		if command == 'register' and 'email' in data and 'pin' in data:
			if self._register(data['email'], data['pin']):
				status = 'WAIT'
				message = "Waiting for response from Polar Cloud"
			else:
				message = "Unable to communicate with Polar Cloud"
		else:
			message = "Unable to understand command"
		return flask.jsonify({'status': status, 'message': message})

	#~~ Slicing profile
	def _create_slicing_profile(self, config_file_bytes):

		class ConfigFileReader(StringIO, object):
			def __init__(self, *args, **kwargs):
				self._dummy_section = True
				self._indent = False
				return super(ConfigFileReader, self).__init__(*args, **kwargs)

			def readline(self):
				if self._dummy_section:
					self._dummy_section = False
					return "[x]"
				line = super(ConfigFileReader, self).readline()
				if self._indent:
					line = "    " + line
				if '"""' in line:
					self._indent = not self._indent
				return line


		def config_file_generator(fp):
			# prepend a dummy section header (x)
			# indent multi-line strings (in triple quotes)
			indent = False
			line = "[x]"
			while line:
				if indent:
					line = "    " + line
				if '"""' in line:
					indent = not not indent
				yield line
				line = fp.readline()

		# create an in memory "file" of the profile and prepend a dummy section
		# header so ConfigParser won't give up so easily
		config_file = ConfigFileReader(config_file_bytes)

		import ConfigParser
		config = ConfigParser.ConfigParser()
		try:
			config.readfp(config_file)
		except:
			self._logger.exception("Error while reading PolarCloud slicing configuration.")
			return None

		printer_profile = self._printer_profile_manager.get_current_or_default()
		extrusion_width = printer_profile["extruder"]["nozzleDiameter"]
		if "extrusionWidth" in config.options("x"):
			extrusion_width = config.get("x", "extrusionWidth")
		layer_height = 0.2
		if "layerThickness" in config.options("x"):
			layer_height = config.get("x", "layerThickness")
		init_layer_height = layer_height
		if "initialLayerThickness" in config.options("x"):
			init_layer_height = config.get("x", "initialLayerThickness")

		posx = 0
		posy = 0
		mm_from_um = lambda x: x / 1000.0
		no_translation = lambda x: x
		width_from_line_count = lambda x: x * extrusion_width
		height_from_layer_count = lambda x: x * layer_height
		bool_from_int = lambda x: not not x

		profile_from_engine_config = {
			"layerthickness":       ("layer_height",       mm_from_um),
			"printspeed":           ("print_speed",        no_translation),
			"supporttype":          ("support_type",       lambda x: "lines" if x == 0 else "grid"),
			"infillspeed":          ("infill_speed",       no_translation),
			"infilloverlap":        ("fill_overlap",       no_translation),
			"filamentdiameter":     ("filament_diameter",  lambda x: [mm_from_um(x) for i in range(4)]),
			"filamentflow":         ("filament_flow",      no_translation),
			"retractionamountextruderswitch": ("retraction_dual_amount", mm_from_um),
			"retractionamount":     ("retraction_amount",  mm_from_um),
			"retractionspeed":      ("retraction_speed",   no_translation),
			"initiallayerthickness":("bottom_thickness",   mm_from_um),
			"extrusionwidth":       ("edge_width",         mm_from_um),
			"insetcount":           ("wall_thickness",     width_from_line_count),
			"downskincount":        ("solid_layer_thickness", height_from_layer_count),
			"upskincount":          ("solid_layer_thickness", height_from_layer_count),
			"initialspeeduplayers": (None, None),          # octoprint always uses 4
			"initiallayerspeed":    ("bottom_layer_speed", no_translation),
			"inset0speed":          ("outer_shell_speed",  no_translation),
			"insetxspeed":          ("inner_shell_speed",  no_translation),
			"movespeed":            ("travel_speed",       no_translation),
			"minimallayertime":     ("cool_min_layer_time",no_translation),
			"infillpattern":        (None, None),          # octoprint doesn't set
			"layer0extrusionwidth": ("first_layer_width_factor", lambda x: mm_from_um(x) * 100.0 / extrusion_width),
			"spiralizemode":        ("spiralize",          bool_from_int),
			"supporteverywhere":    ("support",            lambda x: "everywhere" if x else "none") ,
			"sparseinfilllinedistance": ("fill_density",   lambda x: 100.0 * extrusion_width / mm_from_um(x)),
			"multivolumeoverlap":   ("overlap_dual",       mm_from_um),
			"enableoozeshield":     ("ooze_shield",        bool_from_int),
			"fanfullonlayernr":     ("fan_full_height",    lambda x: (x - 1) * layer_height + init_layer_height),
			"gcodeflavor":          ("gcode_flavor",       lambda x: "reprap"), # TODO: GPX -> RepRap
			"autocenter":           (None, None),          # octoprint doesn't set
			"objectsink":           ("object_sink",        mm_from_um),
			"extruderoffset[0].x":  (None, None),          # octoprint always overrides with printer profile
			"extruderoffset[0].y":  (None, None),          # octoprint always overrides with printer profile
			"retractionminimaldistance": ("retraction_min_travel", mm_from_um),
			"retractionzhop":       ("retraction_hop",     mm_from_um),
			"minimalextrusionbeforeretraction": ("rectraction_minimal_extrusion", mm_from_um),
			"enablecombing":        ("retraction_combing", lambda x: "all" if x == 1 else ("no skin" if x == 2 else "off")),
			"minimalfeedrate":      ("cool_min_feedrate",  no_translation),
			"coolheadlift":         ("cool_head_lift",     bool_from_int),
			"fanspeedmin":          ("fan_speed",          no_translation),
			"fanspeedmax":          ("fan_speed_max",      no_translation),
			"skirtdistance":        ("skirt_gap",          mm_from_um),
			"skirtminlength":       ("skirt_minimal_length", mm_from_um),
			"skirtlinecount":       ("skirt_line_count",   no_translation),
			"supportangle":         ("support_angle",      no_translation),
			"supportxydistance":    ("support_xy_distance", mm_from_um),
			"supportzdistance":     ("support_z_distance", mm_from_um),
			"supportlinedistance":  ("support_fill_rate",  lambda x: 100.0 * extrusion_width / mm_from_um(x)),
			"startcode":            ("start_gcode",        lambda x: ["(@ignore {print_temperature})\n(@ignore {print_bed_temperature})\n" + x[3:-3]]),
			"endcode":              ("end_gcode",          lambda x: [x[3:-3]])
		}

		profile = dict()
		posx = 0
		posy = 0
		for option in config.options("x"):
			# try to fetch the value in the correct type
			try:
				value = config.getint("x", option)
			except:
				# no int, try float
				try:
					value = config.getfloat("x", option)
				except:
					# no float, use str
					value = config.get("x", option)

			if option in profile_from_engine_config:
				key, translate = profile_from_engine_config[option]
				if key:
					profile[key] = translate(value)
				else:
					self._logger.debug("Eating PolarCloud setting {}={}".format(option, value))
			elif option == "fixhorrible":
				profile["fix_horrible_union_all_type_a"] = not not (value & 0x01)
				profile["fix_horrible_union_all_type_b"] = not not (value & 0x02)
				profile["fix_horrible_extensive_stitching"] = not not (value & 0x04)
				profile["fix_horrible_use_open_bits"] = not not (value & 0x10)
			elif option == "posx":
				posx = mm_from_um(value)
			elif option == "posy":
				posy = mm_from_um(value)
			else:
				self._logger.warn("PolarCloud slicing profile contains unrecognized setting {}={}".format(option, value))

		self._logger.debug("Profile looks like this: {}".format(repr(profile)))
		profile["fan_enabled"] = "fan_speed_max" in profile and profile["fan_speed_max"] > 0

		profile = self._slicing_manager.save_profile("cura", "polarcloud", profile,
				allow_overwrite=True, display_name="PolarCloud",
				description="Polar Cloud sends this slicing profile down with each cloud print (overwritten each time)")
		return (profile, (posx, posy))

	def strip_ignore(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		if cmd and cmd.startswith("(@ignore"):
			return None,

	#~~ Timelapse

class PolarTimelapseTranscoder(object):

	def __init__(self, octoprint_movie, callback, logger):
		self._octoprint_movie = octoprint_movie
		movie_basename, ext = os.path.splitext(octoprint_movie)
		self._polar_movie = movie_basename + ".mp4"
		self._callback = callback
		self._logger = logger

	def translate_timelapse(self):
		self._thread = threading.Thread(target=self._translate_timelapse_worker,
				name="PolarCloudTimelapseJob_{octoprint_movie}".format(octoprint_movie=self._octoprint_movie))
		self._thread.daemon = True
		self._thread.start()

	# working thread for converting from OctoPrint's timelapse format to PolarCloud's
	def _translate_timelapse_worker(self):
		command = 'gst-launch-1.0 -e filesrc location="{infile}" ! decodebin name=decode ! x264enc ! queue ! qtmux name=mux ! filesink location={outfile} decode. ! mux.'.format(
				infile=self._octoprint_movie, outfile=self._polar_movie)
		self._logger.debug("timelapse command: {}".format(command))

		try:
			p = sarge.run(command, stdout=sarge.Capture(), stderr=sarge.Capture())
			if p.returncode != 0:
				self._logger.warn("Could not render movie, got return code {returncode}: {stderr_text}".format(returncode=p.returncode, stderr_text=p.stderr.text))
			else:
				self._logger.debug("gstreamer succeded: {}".format(p.stdout.text))
				self._callback(self._polar_movie)

		except:
			self._logger.exception("Could not render movie due to unknown error")

__plugin_name__ = "PolarCloud"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PolarcloudPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.strip_ignore
	}

