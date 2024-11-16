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
from functools import reduce
try:
	import queue
except ImportError:
	import Queue as queue
import base64
import datetime
from time import sleep
import io
from io import StringIO, BytesIO
try:
	from urllib.parse import urlparse, urlunparse
except ImportError:
	from urlparse import urlparse, urlunparse
import random
import re
import json

from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15
from Cryptodome.Hash import SHA256

import socketio
import sarge
import flask
from flask_babel import gettext, _
import requests
from PIL import Image

import octoprint.plugin
import octoprint.util
import octoprint_client
from octoprint.util import get_exception_string
from octoprint.events import Events
from octoprint.filemanager import FileDestinations
from octoprint.filemanager.util import StreamWrapper
from octoprint.slicing.exceptions import UnknownSlicer, SlicerNotConfigured

# logging.getLogger('socketIO-client').setLevel(logging.DEBUG)
# logging.basicConfig()

# what's a mac address we can use as an identifier?
def get_mac():
	return ':'.join(('%012X' % uuid.getnode())[i:i+2] for i in range(0, 12, 2))

# what's the likely ip address for the local UI?
def get_ip():
	return octoprint.util.address_for_client("google.com", 80)

# take a server relative or localhost url and attempt to make absolute an absolute
# url out of it python-socketio(guess about which interface)
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

# compute total filament length from all tools
def filament_length_from_job_data(data):
	filament_length = 0
	if "job" in data and "filament" in data["job"] and isinstance(data["job"]["filament"], dict):
		for tool, tool_info in data["job"]["filament"].items():
			if "length" in tool_info:
				filament_length += tool_info["length"]
	return filament_length


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
		self._task_queue = queue.Queue()
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
		self._disconnect_on_unregister = False
		self._hello_sent = False
		self._port = 80
		self._octoprint_client = None
		self._capabilities = None
		self._next_pending = False
		self._print_preparer = None
		self._status = None
		self._email = None
		self._pin = None
		# consider temp reads higher than this as having a target set for more
		# frequent reports
		self._set_temp_threshold = 50
		self._sent_command_list = None

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self, *args, **kwargs):
		return dict(
			service="https://printer4.polar3d.com",
			service_ui="https://polar3d.com",
			serial=None,
			machine_type="Cartesian",
			printer_type="Cartesian",
			email="",
			pin="",
			max_image_size = 150000,
			verbose=False,
			upload_timelapse=True,
			enable_system_commands=True,
			next_print=False
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
		if self._socket and self._hello_sent:
			self._task_queue.put(self._custom_command_list)

	##~~ AssetPlugin mixin

	def get_assets(self, *args, **kwargs):
		return dict(
			js=["js/polarcloud.js"],
			css=["css/polarcloud.css"],
			less=["less/polarcloud.less"]
		)

	##~~ Softwareupdate hook

	def get_update_information(self, *args, **kwargs):
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

	def on_startup(self, host, port, *args, **kwargs):
		self._port = port

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
			socketioLogging = self._settings.get(['verbose'])
			self._socket = socketio.Client(logger=socketioLogging, engineio_logger=socketioLogging)
		except:
			self._socket = None
			self._logger.exception('Unable to open socket {}'.format(get_exception_string()))
			return

		# Register all the socket messages
		self._socket.on('disconnect', self._on_disconnect)
		self._socket.on('registerResponse', self._on_register_response)
		self._socket.on('welcome', self._on_welcome)
		self._socket.on('capabilitiesResponse', self._on_capabilities_response)
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
		self._socket.on('jogPrinter', self._on_jog_printer)
		self._socket.on('unregisterResponse', self._on_unregister_response)
		self._socket.connect(self._settings.get(['service']))


	def _start_polar_status(self):
		if self._polar_status_worker:
			# try to avoid a race by giving time for is_alive to become true for
			# a just created thread
			self._polar_status_worker.join(0.2)
		if not self._polar_status_worker or not self._polar_status_worker.is_alive():
			self._logger.debug("starting heartbeat")
			self._polar_status_worker = threading.Thread(target=self._polar_status_heartbeat)
			self._polar_status_worker.daemon = True
			self._polar_status_worker.start()

	def _stop_polar_status(self):
		if self._polar_status_worker:
			self._shutdown = True

	def _system(self, command_line):
		try:
			p = sarge.run(command_line, stderr=sarge.Capture())
			return (p.returncode, p.stderr.text)
		except:
			self._logger.exception("Failed to run system command: {}".format(command_line))
			return (1, "")

	def _generate_key(self, key_filename):
		try:
			self._logger.info('Generating key pair')
			key = RSA.generate(2048)
			with open(key_filename, 'wb') as f:
				f.write(key.export_key('PEM'))
			if sys.platform != 'win32':
				os.chmod(key_filename, stat.S_IRUSR | stat.S_IWUSR)
		except:
			self._logger.exception("Unable to generate and save new private key")

	def _get_keys(self, force_regen = False):
		data_folder = self.get_plugin_data_folder()
		key_filename = os.path.join(data_folder, 'p3d_key')
		self._logger.debug('key_filename: {}'.format(key_filename))
		if force_regen or not os.path.isfile(key_filename):
			self._generate_key(key_filename)
		try:
			with open(key_filename) as f:
				key = f.read()
			if force_regen and len(key) <= 0:
				self._logger.warn("Found zero length key, generating a new key")
				self._generate_key(key_filename)
				with open(key_filename) as f:
					key = f.read()
			self._key = RSA.import_key(key)
		except:
			self._key = None
			self._logger.exception("Unable to generate or access key.")
			return

		try:
			self._public_key = self._key.public_key().export_key().decode('utf-8')
		except:
			self._logger.info("Unable to get public key via export_key, reading .pub file")
			pubkey_filename = key_filename + ".pub"
			if not os.path.isfile(pubkey_filename) or os.path.getsize(pubkey_filename) == 0:
				if sys.platform != 'win32':
					os.chmod(key_filename, stat.S_IRUSR | stat.S_IWUSR)
				self._logger.info("Unable to read .pub file, attempting ssh-keygen")
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
			"NONE": self.PSTATE_ERROR,
			"FINISHING": self.PSTATE_POSTPROCESSING
		}
		# this is a bit complicated because the mapping isn't direct and while
		# we try to keep track of current polar state, current octoprint state
		# wins, so we let _pstate show through if it "matches" current octoprint
		if self._cloud_print:
			if self._pstate_counter:
				if self._next_pending and self._pstate == self.PSTATE_COMPLETE:
					self._next_pending = False
					self._task_queue.put(self._send_next_print)
				# if we've got a counter, we're still repeating completion/cancel
				# message, do that
				self._pstate_counter -= 1
				pstate = self._pstate
				if not self._pstate_counter:
					if self._pstate == self.PSTATE_POSTPROCESSING:
						self._pstate = self.PSTATE_COMPLETE
						self._pstate_counter = 3
					else:
						self._cloud_print = False
				return pstate
			if self._pstate == self.PSTATE_POSTPROCESSING:
				return self._pstate

		self._logger.debug("OctoPrint state: {}".format(self._printer.get_state_id()))
		state_id = self._printer.get_state_id()
		state = self.PSTATE_ERROR
		try:
			state = state_mapping[state_id]
		except KeyError:
			self._logger.exception("Unknown OctoPrint status, mapping to error state for PolarCloud: {}".format(state_id))

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
			if state != self.PSTATE_PAUSED and state != self.PSTATE_POSTPROCESSING:
				# if we aren't preparing, printing, finished or paused and we're not
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
		if 'tool1' in temps and not (temps['tool1']['actual'] == -1 and temps['tool1']['target'] == 0):
			status['tool1'] = temps['tool1']['actual']
			status['targetTool1'] = temps['tool1']['target']
			if status['targetTool1'] > 0 or status['tool1'] > self._set_temp_threshold:
				target_set = True
		if 'bed' in temps and not (temps['bed']['actual'] == -1 and temps['bed']['target'] == 0):
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
			status["filamentUsed"] = filament_length_from_job_data(data)
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
						except queue.Empty:
							pass
					if not ignore_status_now and self._status_now:
						self._status_now = False
						self._logger.debug("_status_now break")
						return False
					self._socket.sleep(1)
					if not self._connected:
						self._socket = None
						return False
					if self._shutdown:
						return False
				return True
			except:
				if not self._shutdown:
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
			self._shutdown = False

			while not self._shutdown:
				self._logger.debug("self._socket: {}".format(repr(self._socket)))
				if self._socket:
					self._logger.debug("_wait_and_process")
					_wait_and_process(10)
				else:
					reconnection_delay = random.uniform(1.5, 3)
					self._logger.warn("unable to create socket to Polar Cloud, check again in {} seconds".format(reconnection_delay))
					try:
						sleep(reconnection_delay)
						self._create_socket()
						self._logger.info("Socket created.")
					except:
						self._logger.exception("Something went wrong trying to create the socket.")

				# wait until we get a hello
				if not self._hello_sent:
					continue

				self._status_now = False
				_wait_and_process(5, True)
				if self._socket:
					self._ensure_upload_url('idle')
					self._custom_command_list()
					self._send_capabilities()
				skip_snapshot = False

				while self._connected:
					status, target_set = self._current_status()
					self._status = status
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
					if self._shutdown:
						return

				self._logger.info("Socket disconnected, clear and restart")
				if status_sent < 3 and not self._disconnect_on_register and not self._disconnect_on_unregister:
					self._logger.warn("Unable to connect to Polar Cloud")
					break
				self._socket = None
				self._logger.debug("bottom of forever")

		except:
			self._logger.exception("heartbeat failure")
			return

	def _on_disconnect(self):
		self._logger.debug("[Disconnected]")
		self._connected = False
		# If unregisterd shutdown worker
		if self._disconnect_on_unregister:
			self._stop_polar_status()
			self._disconnect_on_unregister = False

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
				buf = BytesIO()
				buf.write(image_bytes)
				image = Image.open(buf)
				image.thumbnail((640, 480))
				if self._settings.global_get(["webcam", "flipH"]):
					image = image.transpose(Image.FLIP_LEFT_RIGHT)
				if self._settings.global_get(["webcam", "flipV"]):
					image = image.transpose(Image.FLIP_TOP_BOTTOM)
				if self._settings.global_get(["webcam", "rotate90"]):
					image = image.transpose(Image.ROTATE_90)
				image_bytes = BytesIO()
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
		if not path:
			return
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
			if not isinstance(self._challenge, bytes):
				self._challenge = self._challenge.encode('utf-8')
			self._task_queue.put(self._hello)

	def _hello(self):
		self._logger.debug('hello')
		if self._serial and self._challenge:
			self._hello_sent = True
			self._status_now = True
			self._logger.debug('emit hello')
			self._machine_type = self._settings.get(["machine_type"])
			self._printer_type = self._settings.get(["printer_type"])
			camUrl = self._settings.global_get(["webcam", "stream"])
			try:
				if camUrl:
					camUrl = normalize_url(camUrl)
			except:
				self._logger.exception("Unable to canonicalize the url {}".format(camUrl))
			self._logger.debug("camUrl: {}".format(camUrl))
			transformImg = 0
			if self._settings.global_get(["webcam", "flipH"]):
				transformImg += 1
			if self._settings.global_get(["webcam", "flipV"]):
				transformImg += 2
			if self._settings.global_get(["webcam", "rotate90"]):
				transformImg += 4
			self._socket.emit('hello', {
				'serialNumber': self._serial,
				'signature': base64.b64encode(pkcs1_15.new(self._key).sign(SHA256.new(self._challenge))).decode('utf-8'),
				'MAC': get_mac(),
				'localIP': get_ip(),
				'protocol': '2',
				'camUrl': camUrl,
				'transformImg': transformImg,
				'machineType': self._machine_type,
				'printerType': self._printer_type
			})
			self._challenge = None
		else:
			self._logger.debug('skip emit hello, serial: {}'.format(self._serial))

	#~~ capabilities -> polar: capabilitiesResponse

	def _on_capabilities_response(self, response, *args, **kwargs):
		self._logger.debug('_on_capabilities_response: {}'.format(repr(response)))
		if 'capabilities' in response:
			self._capabilities = response['capabilities']

	def _send_capabilities(self):
		self._socket.emit('capabilities', {
			'serialNumber': self._serial,
		})

	def _send_next_print(self):
		if self._settings.get_boolean(['next_print']):
			self._logger.debug("emit sendNextPrint")
			self._socket.emit('sendNextPrint', {
				'serialNumber': self._serial
			})

	#~~ register -> polar: registerReponse

	def _on_register_response(self, response, *args, **kwargs):
		self._logger.debug('on_register_response: {}'.format(repr(response)))
		if 'serialNumber' in response:
			self._serial = response['serialNumber']
			self._settings.set(['serial'], self._serial)
			self._settings.set(['email'], self._email)
			self._settings.set(['pin'], self._pin)
			self._settings.save()
			self._status_now = True
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'registration_success',
				'serial': self._serial,
				'email': self._email,
				'pin': self._pin,
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
			self._get_keys(True)
		if not self._key:
			self._logger.info("Can't register because unable to generate signing key")
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'registration_failed',
				'reason': _('The plugin failed to generate a signing key. Please see troubleshooting tips in the <A href="https://github.com/markwal/OctoPrint-PolarCloud/blob/master/README.md">README</A>.')
			})
			return False

		if not self._socket:
			self._start_polar_status()
			sleep(8) # give the thread a moment to start communicating
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
				"protocolVersion": "2",
				"machineType": self._settings.get(["machine_type"]),
				"printerType": self._settings.get(["printer_type"]),
			}
		})
		return True

	#~~ unregister -> polar: unregisterReponse

	def _on_unregister_response(self, response, *args, **kwargs):
		self._logger.debug('on_unregister_response: {}'.format(repr(response)))
		if response['status'] == 'SUCCESS':
			self._settings.set(['serial'], '')
			self._settings.set(['email'], '')
			self._settings.set(['pin'], '')
			self._settings.save()
			self._status_now = True
			self._serial = None
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'unregistration_success',
			})
			self._disconnect_on_unregister = True
			self._socket.disconnect()
		else:
			self._plugin_manager.send_plugin_message(self._identifier, {
				'command': 'unregistration_failed',
				'reason': response['message']
			})

	def _unregister(self):
		if not self._socket:
			self._start_polar_status()
			sleep(8) # give the thread a moment to start communicating
			self._logger.debug("Do we have a socket: {}".format(repr(self._socket)))
		if not self._socket:
			self._logger.info("Can't unregister because unable to communicate with Polar Cloud")
			return False

		self._logger.info("emit unregister")
		self._socket.emit("unregister", {
			"serialNumber": self._serial,
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
	def _get_slicer_name(self):
		slicer = 'curalegacy'
		if 'Printrbelt' in self._printer_type:
			printrbelt_slicer = 'printrbelt-cura'
			try:
				self._slicing_manager.get_slicer(printrbelt_slicer)
				slicer = printrbelt_slicer
			except (UnknownSlicer, SlicerNotConfigured):
				pass
		return slicer

	def _on_print(self, data, *args, **kwargs):
		self._logger.debug("on_print {0}".format(repr(data)))
		if not self._valid_packet(data):
			return
		if self._print_preparer and self._print_preparer.is_alive():
			self._logger.warn("PolarCloud sent a print command, but the plugin thinks we're still slicing.")
			return

		if self._printer.is_printing() or self._printer.is_paused():
			self._logger.warn("PolarCloud sent print command, but OctoPrint is already printing.")
			return

		self._job_id = "123"
		stl = False
		gcode = False
		threemf = False
		ext = '.stl'
		print_file = ''
		if 'threemfFile' in data:
			threemf = True
			print_file = data['threemfFile']
			ext = '.3mf'
		elif 'gcodeFile' in data:
			gcode = True
			print_file = data['gcodeFile']
			ext = '.gcode'
		elif 'stlFile' in data:
			stl = True
			print_file = data['stlFile']
		else:
			self._logger.warn("PolarCloud sent print command without a print file path.")
			return
		self._logger.debug("PolarCloud requested to print {}. Downloading to a file with ext: {}.".format(print_file, ext))

		info = {}
		pos = (0, 0)
		slicer = 'curalegacy'
		if stl:
			self._logger.debug("Checking slicer configuration.")
			# need to slice then, so make sure we're set up to do that
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
			slicer = self._get_slicer_name()
			slicing_profile = None
			try:
				(slicing_profile, pos) = self._create_slicing_profile(slicer, req_ini.content)
			except (UnknownSlicer, SlicerNotConfigured):
				#TODO tell PolarCloud that we don't have a slicer so it can tell the user
				pass
			if slicing_profile is None:
				self._logger.warn("Unable to create slicing profile. Aborting slice and print.")
				return

		# get the print_file from the cloud
		# TODO: use tornado async I/O to get the print file?
		try:
			info['file'] = print_file
			req_stl = requests.get(print_file, timeout=5)
			req_stl.raise_for_status()
		except Exception:
			self._logger.exception("Could not retrieve print file from PolarCloud: {}".format(print_file))
			return

		path = self._file_manager.add_folder(FileDestinations.LOCAL, "polarcloud")
		path = self._file_manager.join_path(FileDestinations.LOCAL, path, "current-print")
		pathGcode = path + ".gcode"
		path = path + ext
		self._logger.debug("Adding PolarCloud download as {}".format(path))
		self._file_manager.add_file(FileDestinations.LOCAL, path, StreamWrapper(path, BytesIO(req_stl.content)), allow_overwrite=True)
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

		def _on_upload_success(filename, full_path, destination):
			self._printer.select_file(full_path, destination == FileDestinations.SDCARD, printAfterSelect=True)

		if threemf:
			# upload the 3mf file to the printer's SD card
			self._printer.add_sd_file(path,
					self._file_manager.path_on_disk(FileDestinations.LOCAL, path), 
					on_success=_on_upload_success)
		elif stl:
			# prepare the gcode file by slicing
			self._print_preparer = PolarPrintPreparer(slicer,
					self._file_manager, path, pathGcode, pos,
					self._on_slicing_complete, self._on_slicing_failed,
					self._logger)
			self._print_preparer.prepare()
		else:
			self._on_slicing_complete(self._file_manager.path_on_disk(FileDestinations.LOCAL, path))

	def _on_slicing_failed(self, e):
		self._logger.exception("Unable to slice.")
		self._pstate = self.PSTATE_ERROR
		self._pstate_counter = 3
		self._print_preparer = None

	def _on_slicing_complete(self, path, *args, **kwargs):
		# TODO store self._cloud_print_info[sliceDetails]
		self._logger.debug("_on_slicing_complete")
		self._pstate = self.PSTATE_PRINTING
		self._printer.select_file(path, False, printAfterSelect=True)
		self._update_interval = 10
		self._status_now = True
		self._print_preparer = None

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

	def _ensure_octoprint_client(self):
		if not self._octoprint_client:
			baseurl = octoprint_client.build_base_url(host="127.0.0.1", port=self._port)
			self._octoprint_client = octoprint_client.Client(baseurl, self._settings.global_get(['api', 'key']))
		return self._octoprint_client

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
		if self._settings.get_boolean(['enable_system_commands']):
			try:
				client = self._ensure_octoprint_client()
				r = client.get("/api/system/commands")
				r.raise_for_status()
				commands_by_type = json.loads(r.content)
				self._logger.debug("commands: {}".format(repr(commands_by_type)))
				for command_group in commands_by_type.values():
					self._logger.debug("command_group: {}".format(repr(command_group)))
					for command in command_group:
						command_list.append(_polar_custom_from_command(command['source'], command))
			except Exception:
				self._logger.exception("Could not retrieve system commands")

		if self._sent_command_list != command_list:
			self._logger.debug("customCommandList")
			self._socket.emit('customCommandList', {
				'serialNumber': self._serial,
				'commandList': command_list
			})
			self._sent_command_list = command_list
			self._logger.debug("customCommandList sent.");
		else:
			self._logger.warn("customCommandList generated, but dropped because no changes detected.");

	def _on_custom_command(self, data, *args, **kwargs):
		self._logger.debug("customCommand: {}".format(repr(data)))
		if not self._valid_packet(data):
			return
		try:
			if not 'command' in data:
				self._logger.warn("Ignoring custom command, no 'command' element: {}".format(repr(data)))
				return
			client = self._ensure_octoprint_client()

			r = client.post("/api/system/commands/" + data['command'], {})
			r.raise_for_status()
			self._logger.debug("system/commands result {}: {}".format(r.status_code, r.content))
		except Exception:
			self._logger.exception("Could not execute system command: {}".format(repr(data)))


	#~~ jogPrinter

	def _on_jog_printer(self, data, *args, **kwargs):
		self._logger.warn("Jog request: {}".format(repr(data)))
		if not 'jogPrinter' in data:
			self._logger.warn("Ignoring jogPrinter command, no jogPrinter in data: {}".format(repr(data)))
			return
		# The data object should have a jogPrinter field which containts
		# the JSON object that the Octopi API is expecting
		jog_data = data['jogPrinter']
		self._logger.debug("Jog command: {}".format(repr(jog_data)))
		api_command = "printhead"
		if jog_data['command'] == 'extrude':
			api_command = "tool"

		client = self._ensure_octoprint_client()
		r = client.post_json("/api/printer/" + api_command, data['jogPrinter'])
		r.raise_for_status()


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
			payload = {
				'serialNumber': self._serial,
				'jobId': job_id,
				'state': state,
			}
			if self._status:
				# send along the stats from the most recent status
				payload['filamentUsed'] = self._status['filamentUsed']
				payload['printSeconds'] = self._status['printSeconds']
			self._logger.debug("job payload: {}".format(payload))
			self._socket.emit('job', payload)
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
				self._next_pending = True
			if self._status and "time" in payload:
				self._status["printSeconds"] = payload["time"]
			self._job(self._job_id, "completed")
		elif event == Events.SLICING_CANCELLED or event == Events.SLICING_FAILED:
			self._pstate = self.PSTATE_CANCELLING
			self._pstate_counter = 3
			if self._status and "time" in payload:
				self._status["printSeconds"] = payload["time"]
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
		elif event == Events.SHUTDOWN:
			self._shutdown = True
			return
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
			register=[],
			unregister=[],
		)

	def is_api_adminonly(self, *args, **kwargs):
		return True

	def on_api_command(self, command, data):
		status='FAIL'
		message=''
		if command == 'register' and 'email' in data and 'pin' in data:
			if 'machine_type' in data:
				self._settings.set(['machine_type'], data['machine_type'])
			if 'printer_type' in data:
				self._printer_type = data['printer_type']
				self._settings.set(['printer_type'], self._printer_type)
			if 'email' in data:
				self._email =  data['email']
			if 'pin' in data:
				self._pin =  data['pin']
			if self._register(data['email'], data['pin']):
				status = 'WAIT'
				message = "Waiting for response from Polar Cloud"
			else:
				message = "Unable to communicate with Polar Cloud"
		elif command == 'unregister':
			if self._unregister():
				status = 'WAIT'
				message = "Waiting for response from Polar Cloud"
			else:
				message = "Unable to communicate with Polar Cloud"
		else:
			message = "Unable to understand command"
		return flask.jsonify({'status': status, 'message': message})

	def on_api_get(self, request):
		return flask.jsonify({'capabilities': self._capabilities})

	#~~ Slicing profile
	def _create_slicing_profile(self, slicer, config_file_bytes):

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

		# create an in memory "file" of the profile and prepend a dummy section
		# header so ConfigParser won't give up so easily
		# DEBUG
		with open("/home/pi/foo.ini", "w") as ini_file:
			ini_file.write(config_file_bytes.decode('utf-8'))
		config_file = ConfigFileReader(config_file_bytes.decode('utf-8'))

		try:
			import configparser
		except ImportError:
			import ConfigParser as configparser
		config = configparser.ConfigParser(interpolation=None)
		try:
			config.readfp(config_file)
		except:
			self._logger.exception("Error while reading PolarCloud slicing configuration.")
			return None

		printer_profile = self._printer_profile_manager.get_current_or_default()

		# get a few ahead of time since other translations are dependent

		# extrusion_width
		extrusion_width = printer_profile["extruder"]["nozzleDiameter"]
		if "extrusionWidth" in config.options("x"):
			extrusion_width = config.getfloat("x", "extrusionWidth") / 1000.0
		elif "extrusion_width" in config.options("x"):
			layer_height = config.getfloat("x", "extrusion_width")

		# layer_height
		layer_height = 0.2
		if "layerThickness" in config.options("x"):
			layer_height = config.getfloat("x", "layerThickness") / 1000
		elif "layer_height" in config.options("x"):
			layer_height = config.getfloat("x", "layer_height")

		# first layer height
		init_layer_height = layer_height
		if "initialLayerThickness" in config.options("x"):
			init_layer_height = config.getfloat("x", "initialLayerThickness") / 1000
		elif "first_layer_height" in config.options("x"):
			init_layer_height = config.getfloat("x", "first_layer_height")

		# support
		support = "None"
		if "support_material" in config.options("x"):
			value = config.getint("x", "support_material")
			if value != 0:
				support = "Touching Buildplate"

		posx = 0
		posy = 0
		mm_from_um = lambda x: x / 1000.0
		no_translation = lambda x: x
		width_from_line_count = lambda x: x * extrusion_width
		height_from_layer_count = lambda x: x * layer_height
		bool_from_int = lambda x: not not x
		self._logger.debug("layer_height={}; height_from_layer_count(3)={}".format(layer_height, height_from_layer_count(3)))

		profile_from_engine_config = {
			"layerthickness":       ("layer_height",       mm_from_um),
			"layer_height":         ("layer_height",       no_translation),
			"printspeed":           ("print_speed",        no_translation),
			"print_speed":          ("print_speed",        no_translation),
			"perimeter_speed":      ("print_speed",        no_translation),
			"supporttype":          ("support_type",       lambda x: "lines" if x == 0 else "grid"),
			"support_material":     ("support",            lambda x: "None" if x == 0 else "Touching Buildplate"),
			"fill_pattern":         (None, None),          # octoprint and legacy cura only support lines and grid
			"infillspeed":          ("infill_speed",       no_translation),
			"infill_speed":         ("infill_speed",       no_translation),
			"solid_infill_speed":   (None, None),          # not sure where to send this one
			"solidtopinfillspeed":  (None, None),          # not sure where to send this one
			"infilloverlap":        ("fill_overlap",       no_translation),
			"infill_overlap":       ("fill_overlap",       no_translation),
			"filamentdiameter":     ("filament_diameter",  lambda x: [mm_from_um(x) for i in range(4)]),
			"filament_diameter":    ("filament_diameter",  lambda x: [x for i in range(4)]),
			"filamentflow":         ("filament_flow",      no_translation),
			"extrusion_multiplyer": ("filament_flow",      no_translation),
			"retractionamountextruderswitch": ("retraction_dual_amount", mm_from_um),
			"retract_length_toolchange": ("retraction_dual_amount", no_translation),
			"retractionamount":     ("retraction_amount",  mm_from_um),
			"retract_length":       ("retraction_amount",  no_translation),
			"retractionspeed":      ("retraction_speed",   no_translation),
			"retract_speed":        ("retraction_speed",   no_translation),
			"initiallayerthickness":("bottom_thickness",   mm_from_um),
			"first_layer_height":   ("bottom_thickness",   no_translation),
			"extrusionwidth":       ("edge_width",         mm_from_um),
			"extrusion_width":      ("edge_width",         no_translation),
			"perimeters":           ("wall_thickness",     width_from_line_count),
			"downskincount":        ("solid_layer_thickness", height_from_layer_count),
			"bottom_solid_layers":  ("solid_layer_thickness", height_from_layer_count),
			"upskincount":          ("solid_layer_thickness", height_from_layer_count),
			"top_solid_layers":     ("solid_layer_thickness", height_from_layer_count),
			"initialspeeduplayers": (None, None),          # octoprint always uses 4
			"initiallayerspeed":    ("bottom_layer_speed", no_translation),
			"first_layer_speed":    ("bottom_layer_speed", no_translation),
			"inset0speed":          ("outer_shell_speed",  no_translation),
			"insetxspeed":          ("inner_shell_speed",  no_translation),
			"movespeed":            ("travel_speed",       no_translation),
			"travel_speed":         ("travel_speed",       no_translation),
			"minimallayertime":     ("cool_min_layer_time",no_translation),
			"infillpattern":        (None, None),          # octoprint doesn't set
			"layer0extrusionwidth": ("first_layer_width_factor", lambda x: mm_from_um(x) * 100.0 / extrusion_width),
			"first_layer_extrusion_width": ("first_layer_width_factor", lambda x: x * 100.0 / extrusion_width),
			"spiralizemode":        ("spiralize",          bool_from_int),
			"spiral_vase":          ("spiralize",          bool_from_int),
			"sparseinfilllinedistance": ("fill_density",   lambda x: 100.0 * extrusion_width / mm_from_um(x)),
			"fill_density":         ("fill_density",       no_translation),
			"multivolumeoverlap":   ("overlap_dual",       mm_from_um),
			"enableoozeshield":     ("ooze_shield",        bool_from_int),
			"ooze_prevention":      ("ooze_shield",        bool_from_int),
			"fanfullonlayernr":     ("fan_full_height",    lambda x: (x - 1) * layer_height + init_layer_height),
			"full_fan_speed_layer": ("fan_full_height",    lambda x: (x - 1) * layer_height + init_layer_height),
			"gcodeflavor":          ("gcode_flavor",       lambda x: "reprap"),
			"gcode_flavor":         ("gcode_flavor",       lambda x: "reprap"),
			"autocenter":           (None, None),          # octoprint doesn't set
			"objectsink":           ("object_sink",        mm_from_um),
			"nozzle_diameter":      (None, None),          # octoprint always overrides with printer profile
			"bed_shape":            (None, None),          # octoprint always overrides with printer profile
			"rect_origin":          (None, None),          # octoprint always overrides with printer profile
			"extruderoffset[0].x":  (None, None),          # octoprint always overrides with printer profile
			"extruderoffset[0].y":  (None, None),          # octoprint always overrides with printer profile
			"retractionminimaldistance": ("retraction_min_travel", mm_from_um),
			"retract_before_travel": ("retraction_min_travel", no_translation),
			"retractionzhop":       ("retraction_hop",     mm_from_um),
			"retract_lift":         ("retraction_hop",     no_translation),
			"minimalextrusionbeforeretraction": ("retraction_minimal_extrusion", mm_from_um),
			"enablecombing":        ("retraction_combing", lambda x: "all" if x == 1 else ("no skin" if x == 2 else "off")),
			"minimalfeedrate":      ("cool_min_feedrate",  no_translation),
			"min_print_speed":      ("cool_min_feedrate",  no_translation),
			"coolheadlift":         ("cool_head_lift",     bool_from_int),
			"fanspeedmin":          ("fan_speed",          no_translation),
			"min_fan_speed":        ("fan_speed",          no_translation),
			"fanspeedmax":          ("fan_speed_max",      no_translation),
			"max_fan_speed":        ("fan_speed_max",      no_translation),
			"skirtdistance":        ("skirt_gap",          mm_from_um),
			"skirt_distance":       ("skirt_gap",          no_translation),
			"skirtminlength":       ("skirt_minimal_length", mm_from_um),
			"min_skirt_length":     ("skirt_minimal_length", no_translation),
			"skirtlinecount":       ("skirt_line_count",   no_translation),
			"skirts":               ("skirt_line_count",   no_translation),
			"supportangle":         ("support_angle",      no_translation),
			"support_material_threshold": ("support_angle",no_translation),
			"supportxydistance":    ("support_xy_distance", mm_from_um),
			"support_material_spacing": ("support_xy_distance", no_translation),
			"support_material_xy_spacing": ("support_xy_distance", no_translation),
			"supportzdistance":     ("support_z_distance", mm_from_um),
			"supportlinedistance":  ("support_fill_rate",  lambda x: 100.0 * extrusion_width / mm_from_um(x)),
			"support_material_buildplate_only": ("support", lambda x: "Touching Buildplate" if support != "None" else "None"),
			"startcode":            ("start_gcode",        lambda x: ["(@ignore {print_temperature})\n(@ignore {print_bed_temperature})\n" + x[3:-3]]),
			"start_gcode":          ("start_gcode",        lambda x: ["(@ignore {print_temperature})\n(@ignore {print_bed_temperature})\n" + x.replace("\\n", "\n")]),
			"endcode":              ("end_gcode",          lambda x: [x[3:-3]]),
			"end_gcode":            ("end_gcode",          lambda x: [x.replace("\\n", "\n")]),
			"raft_layers":          ("raft_thickness",     height_from_layer_count),
			"raftmargin":           ("raft_margin",        mm_from_um),
			"raftlinespacing":      ("raft_line_spacing",  mm_from_um),
			"raftbasethickness":    ("raft_base_thickness",mm_from_um),
			"raftbaselinewidth":    ("raft_base_linewidth",mm_from_um),
			"raftinterfacethickness": ("raft_thickness",   mm_from_um),
			"raftinterfacelinewidth": ("raft_margin",      mm_from_um),
			"raftinterfacelinespacing": (None, None),      # octoprint computes from linewidth
			"raftbasespeed":        (None, None),          # octoprint always uses bottom_layer_speed
			"raftfanspeed":         (None, None),          # octoprint forces this to 0
			"raftsurfacethickness": ("raft_surface_thickness", mm_from_um),
			"raftsurfacelinewidth": ("raft_surface_linewidth", mm_from_um),
			"raftsurfacelinespacing": (None, None),        # octoprint computes from linewidth
			"raftsurfacelayers":    ("raft_surface_layers",no_translation),
			"raftsurfacespeed":     (None, None),          # octoprint always uses bottom_layer_speed
			"raftairgap":           ("raft_airgap_all",    mm_from_um),
			"raftairgaplayer0":     (None, None),          # octoprint doesn't support a different airgap for layer0)
			"filament_cost":        (None, None),          # octoprint doesn't use these filament infos
			"filament_density":     (None, None),          # octoprint doesn't use these filament infos
		}

		profile = dict()
		profile["support"] = "none"
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
					if value.endswith("%"):
						value = value[:len(value)-1]
						try:
							value = float(value)
						except ValueError:
							# leave it as a string
							pass

			if option in profile_from_engine_config:
				key, translate = profile_from_engine_config[option]
				if key:
					self._logger.debug("key={}; value={}, {}".format(key, value, type(value)))
					profile[key] = translate(value)
					if "raft" in key:
						profile["platform_adhesion"] = "raft"
					elif "support" in key:
						if profile["support"] != "everywhere":
							profile["support"] = "buildplate"
				else:
					self._logger.debug("Eating PolarCloud setting {}={}".format(option, value))
			elif option == "supporteverywhere":
				if value:
					profile["support"] = "everywhere"
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

		try:
			profile = self._slicing_manager.save_profile(slicer, "polarcloud", profile,
					allow_overwrite=True, display_name="PolarCloud",
					description="Polar Cloud sends this slicing profile down with each cloud print (overwritten each time)")
		except:
			self._logger.exception("save_profile failed")
			profile = None
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
			self._callback(None)

	#~~ Slicing

class PolarPrintPreparer(object):
	def __init__(self, slicer, file_manager, path, pathGcode, pos, callback, callback_failed, logger):
		self._slicer = slicer
		self._file_manager = file_manager
		self._path = path
		self._pathGcode = pathGcode
		self._pos = pos
		self._callback = callback
		self._callback_failed = callback_failed
		self._logger = logger
		self._thread = None

	def prepare(self):
		self._thread = threading.Thread(target=self._preparation_worker)
		self._thread.daemon = True
		self._thread.start()

	def is_alive(self):
		return not self._thread or self._thread.is_alive()

	# working thread for slicing
	def _preparation_worker(self):
		try:
			self._file_manager.slice(self._slicer,
					FileDestinations.LOCAL, self._path,
					FileDestinations.LOCAL, self._pathGcode,
					position=self._pos, profile="polarcloud",
					callback=self._callback,
					callback_args=(self._file_manager.path_on_disk(FileDestinations.LOCAL, self._pathGcode),))
		except:
			self._logger.exception("_file_manager.slice failed")
			self._callback_failed()

__plugin_name__ = "PolarCloud"
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PolarcloudPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.strip_ignore
	}
