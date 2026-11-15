<img src="kiosk_puck_bridge/icon.png" alt="" width="64" height="64" align="right">

# KioskMuse Bridge

Use one wake word across EchoMuse pucks and Kiosk Satellites, with one device answering when both systems hear it.

[![Add the KioskMuse Bridge repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvibribbonee%2Fkiosk-puck-bridge)

## Features

- Find Kiosk Satellites through their existing wake arbitration.
- Connect to stock EchoMuse as a single virtual puck.
- Tune how Kiosk and puck microphone levels compare.

## How it works

The winning device runs its normal Assist conversation and plays the answer. The bridge handles wake claims and arbitration.

<details>
<summary>Show the wake arbitration diagram</summary>

```mermaid
flowchart TD
    K["Kiosk Satellites"] -->|Wake claims| B["KioskMuse Bridge"]
    B -->|One virtual puck claim| E{"EchoMuse picks a winner"}
    P["EchoMuse pucks"] -->|Wake claims| E
    E -->|Physical puck wins| R["Bridge tells hearing Kiosks to stand down"]
    E -->|Physical puck wins| EP["Winning EchoMuse puck"]
    E -->|Virtual puck wins| KS["Kiosks pick their winner"]
    KS --> KP["Winning Kiosk Satellite"]
    EP -->|Voice request| H["Home Assistant Assist"]
    KP -->|Voice request| H
```

</details>

## Setup

1. Run the bridge and stock EchoMuse on the same Home Assistant OS host. Enable on-device wake detection and arbitration in EchoMuse. Use the same wake word on both systems.
2. Enable **Wake word arbitration** on each Kiosk. Start with the default **400 ms** window.
3. Add the app repository using the button above. Start **KioskMuse Bridge**, then approve the pending `KIOSK-…` puck in **EchoMuse**. The app log says **Virtual puck is ready to arbitrate** when it can participate.
4. In Home Assistant, choose **Ignore** for **Bridge Voice Assistant** and **Bridge BT Proxy**. If you already added the virtual Voice Assistant, disable that ESPHome entry so the bridge can connect.

![The virtual Bridge Voice Assistant and Bridge BT Proxy cards with their Ignore buttons highlighted](docs/ignore-bridge-discovery.png)

## Tune

Try the wake word near each device. If Kiosks win too often, lower `level_offset_db` in the bridge by 5 dB. Raise it if the pucks win too often. Restart the bridge after changing its settings. For one Kiosk, use its own **Arbitration offset**.

A late claim or verdict can cause two replies. If that happens regularly, try a **450–500 ms** window on the affected Kiosk and check the bridge logs.

If the virtual puck shows **Waiting for HA** under **Bluetooth proxy**, [turn off its unused proxy](kiosk_puck_bridge/DOCS.md#echomuse-status).

[App documentation and troubleshooting](kiosk_puck_bridge/DOCS.md)

## Contributing

Pull requests are welcome. Describe the change and how you tested it.

[MIT license](LICENSE). OpenAI Codex assisted with development, testing, and documentation.
