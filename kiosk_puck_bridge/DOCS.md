# KioskMuse Bridge

KioskMuse Bridge coordinates wake word arbitration between EchoMuse and Kiosk
Satellite. See the [project README](https://github.com/vibribbonee/kiosk-puck-bridge#readme)
for installation, setup, and an explanation of how the bridge works.

## Quick setup

1. Run the stock EchoMuse controller and this app on the same Home Assistant
   OS host. Enable on-device wake detection and wake arbitration in EchoMuse.
2. In Kiosk Satellite, enable **Wake word arbitration** on each device and
   use the same wake word as the EchoMuse pucks.
3. Start this app and approve the single `KIOSK-…` puck in EchoMuse. Wait for
   **Virtual puck is ready to arbitrate** in the app log.

In **Settings → Devices & services**, choose **Ignore** for the virtual puck’s **Bridge Voice Assistant** and **Bridge BT Proxy** ESPHome discoveries. The bridge uses the Voice Assistant connection itself. Keep the integrations for your physical pucks and satellites.

If you already added the virtual Voice Assistant to Home Assistant, disable that virtual ESPHome entry so the bridge can connect.

## EchoMuse status

On the virtual puck’s **Status** tab, **Voice assistant → HA connected** means the bridge has connected to its voice endpoint. Check the bridge log for **Virtual puck is ready to arbitrate**.

If **Bluetooth proxy → Waiting for HA** appears, the virtual puck has inherited an enabled proxy or has one enabled in its own settings. Open the virtual puck’s **Config → Bluetooth**, select **Device** scope, turn off **Bluetooth proxy**, and select **Push config**. The virtual puck has no Bluetooth radio.

EchoMuse can show **Update available** for the virtual puck because it compares its bridge version to physical Echo firmware. Update KioskMuse Bridge from **Home Assistant → Settings → Apps**.

## Configuration and troubleshooting

- **`level_offset_db`** adjusts how Kiosk wake levels compare with EchoMuse
  wake levels. The default is `-55`. Lower it if Kiosks win too often, or
  raise it if the pucks win too often. Restart the app after saving. To
  tune just one Kiosk, use its **Arbitration offset** under **Voice Satellite
  → Wake Word → Wake Word Arbitration**. This adjusts that Kiosk’s claim
  before the bridge receives it.
- **`mode: observe`** logs Kiosk claims without entering the contest.
  **`mode: bridge`** enables arbitration and is the default.
- The controller host and ports default to the standard local EchoMuse
  settings. If mDNS is unavailable, `virtual_puck_port` can specify the
  virtual ESPHome endpoint’s port.

The bridge uses local UDP port 2330 for Kiosk discovery and claims.
If both systems reply, verify that both heard the same phrase and that the
Kiosk arbitration window is at least its 400 ms default. Late detection or
Wi-Fi delivery can put a claim outside the arbitration window.

EchoMuse pairing credentials stay in the app’s Home Assistant data
directory. A store-installed copy has its own data and virtual puck identity.
When migrating from a local copy, stop it before starting the store copy,
then approve the new virtual puck in EchoMuse.
