/*
 * View model for OctoPrint-PolarCloud
 * Copyright (c) 2017 by Mark Walker
 *
 * Author: Mark Walker (markwal@hotmail.com)
 * License: AGPLv3
 *
 * This file is part of OctoPrint-PolarCloud.
 *
 * OctoPrint-PolarCloud is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or (at your
 * option) any later version.
 *
 * OctoPrint-PolarCloud is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
 * or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public
 * License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OctoPrint-PolarCloud.  If not, see <http://www.gnu.org/licenses/agpl.html>
 */
$(function() {
    function PolarcloudViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];
        self.emailAddress = ko.observable("");
        self.pin = ko.observable("");
        self.registering = ko.observable(false);
        self.registrationFailed = ko.observable(false);
        self.registrationFailedReason = ko.observable("");
        self.unregistering = ko.observable(false);
        self.unregistrationFailed = ko.observable(false);
        self.unregistrationFailedReason = ko.observable("");
        self.machineTypes = ko.observableArray();
        self.printerTypes = ko.observableArray();
        self.isPrinterTypesLoading = ko.observable(false);
        self.machineType = ko.observable("");
        self.printerType = ko.observable("");

        self.machineType.subscribe(function (value) {
            if (value) {
                self.settings.machine_type(value);
            }
        });

        self.printerType.subscribe(function (value) {
            if (value) {
                self.settings.printer_type(value);
            }
        });

        self.onBeforeBinding = function() {
            self.settings = self.settingsViewModel.settings.plugins.polarcloud;
        };

        self.printerTypeOptionsValue = function(item) {
            return item == "Other/Custom" ? self.settings.machine_type() : item;
        }

        self.printerTypeOptionsText = function(item) {
            return item;
        }

        self.loadPrinterTypes = function(machine_type, printer_type) {
            self.isPrinterTypesLoading(true);
            $.ajax(self.settings.service_ui() + "/api/v1/printer_makes?filter=" + machine_type.toLocaleLowerCase(), { headers: "" })
                .done(function(response) {
                    if ("printerMakes" in response) {
                        self.isPrinterTypesLoading(false);
                        var options = response["printerMakes"];
                        options.push("Other/Custom");
                        self.printerTypes(options);
                        if (printer_type) {
                            self.printerType(printer_type);
                        }
                    }
                });
        }

        self.onSettingsShown = function() {
            var initialMachineType = self.settings.machine_type();
            var initialPrinterType = self.settings.printer_type();
            $.ajax(self.settings.service_ui() + "/api/v1/printer_makes?filter=octoprint-build", { headers: "" })
                .done(function(response) {
                    if ("printerMakes" in response) {
                        self.machineTypes(response["printerMakes"]);
                        self.machineType(initialMachineType);
                        self.loadPrinterTypes(initialMachineType, initialPrinterType);
                    }
                });
        };

        self.onChangeMachineType = function(obj, event) {
            if (event.originalEvent) {
                self.loadPrinterTypes(self.machineType(), null);
            }
        }

        self.changeRegistrationStatus = function() {
            if(self.settings.serial()){
                $("#plugin_polarcloud_unregistration").modal("show");
            } else {
                $("#plugin_polarcloud_registration").modal("show");
            }
        };

        self.registerPrinter = function() {
            if (self.registering())
                return;
            self.registering(true);
            self.registrationFailed(false);
            setTimeout(function() {
                if (self.registering()) {
                    self.registering(false);
                    self.registrationFailed(true);
                    self.registrationFailedReason("Couldn't connect to the Polar Cloud.");
                }
            }, 10000);
            OctoPrint.simpleApiCommand("polarcloud", "register", {
                "email": self.emailAddress(),
                "pin": self.pin(),
                "printer_type": self.settings.printer_type(),
                "machine_type": self.settings.machine_type()
            }).done(function(response) {
                console.log("polarcloud register response" + JSON.stringify(response));
            });
        };

        self.unregisterPrinter = function() {
            if (self.unregistering())
                return;
            self.unregistering(true);
            self.unregistrationFailed(false);
            setTimeout(function() {
                if (self.unregistering()) {
                    self.unregistering(false);
                    self.unregistrationFailed(true);
                    self.unregistrationFailedReason("Couldn't connect to the Polar Cloud.");
                }
            }, 10000);
            OctoPrint.simpleApiCommand("polarcloud", "unregister", {
                "serialNumber": "test",
            }).done(function(response) {
                console.log("polarcloud unregister response" + JSON.stringify(response));
            });
        }

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin != "polarcloud")
                return;

            if (data.command == "registration_failed") {
                self.registering(false);
                self.registrationFailed(true);
                self.registrationFailedReason(data.reason);
                return;
            }

            if (data.command == "registration_success") {
                self.settings.serial(data.serial);
                self.settings.email(data.email);
                self.settings.pin(data.pin);
                self.registering(false);
                $("#plugin_polarcloud_registration").modal("hide");
                return
            }

            if (data.command == "unregistration_failed") {
                self.unregistering(false);
                self.unregistrationFailed(true);
                self.unregistrationFailedReason(data.reason);
                return;
            }

            if (data.command == "unregistration_success") {
                self.unregistering(false);
                self.settings.serial('');
                self.settings.email('');
                self.settings.pin('');
                $("#plugin_polarcloud_unregistration").modal("hide");
            }
        };
    }

    // view model class, parameters for constructor, container to bind to
    OCTOPRINT_VIEWMODELS.push([
        PolarcloudViewModel,
        [ "settingsViewModel" ],
        [ "#settings_plugin_polarcloud" ]
    ]);
});
