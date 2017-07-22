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
        self.printerTypes = ko.observableArray();
        self.nextPrintAvailable = ko.observable(false);

        self._ensureCurrentPrinterType = function() {
            if (self.printerTypes().indexOf(self.settings.printer_type()) < 0)
                self.printerTypes.push(self.settings.printer_type());
        };

        self.onBeforeBinding = function() {
            self.settings = self.settingsViewModel.settings.plugins.polarcloud;
            self._ensureCurrentPrinterType();
        };

        self.onSettingsShown = function() {
            $.ajax(self.settings.service_ui() + "/api/v1/printer_makes", { headers: "" })
                .done(function(response) {
                    if ("printerMakes" in response) {
                        self.printerTypes(response["printerMakes"]);
                        self._ensureCurrentPrinterType();
                    }
                });
            self.nextPrintAvailable(false);
            OctoPrint.get("polarcloud")
                .done(function(response) {
                    if ("capabilities" in response && "sendNextPrint" in response["capabilities"]) {
                        self.nextPrintAvailable(true);
                    }
                });
        };

        self.showRegistration = function() {
            self.emailAddress(self.settings.email());
            console.log(JSON.stringify(self.emailAddress()));
            $("#plugin_polarcloud_registration").modal("show");
        };

        self.registerPrinter = function() {
            if (self.registering())
                return;
            self.settings.email(self.emailAddress())
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
                "pin": self.pin()
            }).done(function(response) {
                console.log("polarcloud register response" + JSON.stringify(response));
            });
        };

        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin != "polarcloud")
                return;

            if (data.command == "registration_failed") {
                self.registering(false);
                self.registrationFailed(true);
                self.registrationFailedReason(data.reason);
                return;
            }

            if (data.command == "serial" && data.serial) {
                self.settings.serial(data.serial);
            }
            self.registering(false);
            $("#plugin_polarcloud_registration").modal("hide");
        };
    }

    // view model class, parameters for constructor, container to bind to
    OCTOPRINT_VIEWMODELS.push([
        PolarcloudViewModel,
        [ "settingsViewModel" ],
        [ "#settings_plugin_polarcloud" ]
    ]);
});
