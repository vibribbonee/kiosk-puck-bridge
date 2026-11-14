# Kiosk Puck Bridge

Share one wake word across EchoMuse pucks and Kiosk Satellites on the same network. Each system can already pick a winner among its own devices. When both systems hear the same wake word, though, each can start an Assist conversation and reply. The bridge lets them coordinate so one device answers.

## Install and set up

You’ll need the stock EchoMuse controller and the bridge on the same Home Assistant OS host. Enable on-device wake detection and wake arbitration in EchoMuse. On each Kiosk Satellite you want to include, turn on **Enable wake word arbitration**, use the same wake word as your EchoMuse pucks, and start with the default 400 ms Kiosk arbitration window.

[![Add the Kiosk Puck Bridge repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fvibribbonee%2Fkiosk-puck-bridge)

1. Open the button above and confirm adding the repository in Home Assistant.
2. Install and start **Kiosk Puck Bridge**.
3. In the EchoMuse dashboard, approve the single pending device named `KIOSK-…`. The bridge discovers your Kiosks automatically. When the connection is ready, the app log says **Virtual puck is ready to arbitrate**.

To add the repository manually, open **Settings → Apps → Install app → ⋮ → Repositories** and enter `https://github.com/vibribbonee/kiosk-puck-bridge`.

If Home Assistant offers to add the virtual puck as an ESPHome device, leave that discovery unconfigured. The bridge uses that connection itself. Your physical devices keep their existing Assist connections.

## Test and tune

Say the wake word and a short question near each device. Check that only the expected device replies. If one side wins too often, open **Kiosk Puck Bridge → Configuration** and adjust `level_offset_db` in 5 dB steps, then restart the app and test again.

| If this happens | Change the offset |
| --- | --- |
| A Kiosk wins when you’re beside an EchoMuse puck | Lower it, for example from `-55` to `-60` |
| An EchoMuse puck wins when you’re beside a Kiosk | Raise it, for example from `-55` to `-50` |

Use the bridge offset when the whole Kiosk group tends to win or lose. If one Kiosk is the outlier, adjust **Arbitration offset** on that device under **Voice Satellite → Wake Word → Wake Word Arbitration**. Raising it gives that Kiosk a stronger vote both against other Kiosks and through the bridge. Lowering it gives that Kiosk a weaker vote. [Kiosk Satellite settings](https://github.com/jxlarrea/kiosk-satellite/blob/main/docs/voice-satellite.md#wake-word-arbitration)

In stock EchoMuse, **Config → Wake word → Arbitration window** controls which wake detections count as the same request. Keep it above zero for devices using the bridge. Use the bridge offset or the individual Kiosk offset to balance loudness. [EchoMuse settings](https://github.com/wilbowes/EchoMuse/blob/main/docs/configuration.md#arbitration-window)

## How it works

The bridge listens for Kiosk wake claims on the local network. It submits the strongest Kiosk claim to EchoMuse as one virtual puck. Kiosk reports loudness relative to its local noise floor, while EchoMuse uses dBFS. The configurable offset lets you balance those readings for your devices and room.

If a physical puck wins, the bridge relays that result so the Kiosks can stand down. If the virtual puck wins, Kiosk Satellite’s own arbitration decides which Kiosk answers. The winning device handles its own audio and Assist pipeline. Both systems run their stock software.

The bridge uses the protocols implemented by both projects today. A late wake claim can still cause a double response, so test both directions after an upgrade. See the [app documentation](kiosk_puck_bridge/DOCS.md) for troubleshooting and advanced options.

## Contributing

Pull requests are welcome! Tell us what you changed and how you tested it.

## AI disclosure

OpenAI Codex assisted with the bridge’s implementation, tests, and documentation. Arbitration was tested against an unmodified EchoMuse controller with simulated clients and exercised on physical devices.
