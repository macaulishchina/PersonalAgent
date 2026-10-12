-- DELL's paired Xiaoai speakers: use standard SBC for stable voice playback.
-- WirePlumber 0.4 monitor properties; leave device volume and auto-connect alone.
bluez_monitor.properties["bluez5.codecs"] = "[ sbc ]"
-- Negotiate 44.1 kHz with these speakers instead of 48 kHz.
bluez_monitor.properties["bluez5.default.rate"] = 44100
