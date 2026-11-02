# Sigen Promotional Tariffs for Home Assistant

Set promotional tariffs on a Sigenergy station from Home Assistant. A promo overrides
the buy (import) and/or sell (export) price for a time window, once, daily or on chosen
weekdays. It's the **Promotional** price feature in the Sigen app, so Sigen's own
optimiser plans around the new prices.

Typical uses:

- **EDF free electricity / Sunday Saver:** set the import price to 0 for the free slot,
  so the battery charges then.
- **Axle VPP events:** set a high export price for an export event so the battery
  exports, or a zero import price for an import event so it charges.
- Anything else an automation can see coming: a calendar, a sensor or a fixed schedule.

> Unofficial. This uses the same private cloud API as the Sigen web app, worked out from
> browser captures. Sigenergy could change it at any time.

## Install

**HACS:** HACS → ⋮ → **Custom repositories** → add `https://github.com/nebb00/sigen-promo`
as type **Integration** → install **Sigen Promotional Tariffs** → restart Home Assistant.

**Manually:** copy `custom_components/sigen_promo` into your config's
`custom_components` folder and restart.

Needs Home Assistant 2025.6 or later.

Then go to **Settings → Devices & services → Add integration → Sigen Promotional
Tariffs**, pick your region and log in with your Sigen app email and password. Leave
**Station ID** empty to use your main station. For an account with several stations,
add the integration once per station and enter each station's ID (shown in the Sigen
app's station settings).

The integration keeps itself logged in; if the password stops working, Home Assistant
asks for it again. To change the region, email or password yourself, use
**⋮ → Reconfigure** on the integration. Deleting the integration logs it out of the
Sigen cloud.

**Configure** on the integration sets how often promos are re-read from the cloud
(5–60 minutes, default 15). Changes made through the integration show up straight away;
the interval only matters for promos changed elsewhere, such as in the Sigen app.

## Regions

Setup asks for your Sigen cloud region: Europe, United States, Asia-Pacific,
Australia / New Zealand or China. **Only Europe has been tested.** The other regions use
the same API pattern, but the Promotional price feature may not exist there. If you try
another region, please open an issue saying whether it worked, and attach the
integration's **Download diagnostics** file (it leaves out your login details). You can
change the region later with **⋮ → Reconfigure**.

## Entities

| Entity | What it shows |
|---|---|
| `sensor.sigen_promotional_tariffs_current_promo` | The promo running now, e.g. `Axle event · Sell 100`, or `None`. Attributes: `label`, `buy`, `sell`, `start`, `end`. |
| `sensor.sigen_promotional_tariffs_next_promo` | When the next promo starts. Attributes: `label`, `end`, `buy`, `sell`. |
| `sensor.sigen_promotional_tariffs_promos` | Number of current and upcoming promos. The `promos` attribute lists them all. |

Promos set in the Sigen app show up too, labelled `Sigen app`. Times use the station's
timezone.

### Labels

Sigen promos have no name, so the integration keeps an optional **label** for each
promo it adds (e.g. `Axle event`). Labels live in Home Assistant only and show in the
sensors and action responses. They also let automations manage "their" promos:
`replace_label` on `add_promo` and `label` on `remove_promo` only touch promos with
that label, so a moved or cancelled event is cleaned up without touching anything else.

## Actions

All actions return the resulting promo list, so you can use `response_variable` in
scripts and automations.

### `sigen_promo.add_promo`

| Field | Required | Meaning |
|---|---|---|
| `start` | yes | When the promo starts. Any date-time, including a timestamp sensor's state. |
| `end` | yes | When it ends. Windows past midnight are split into one promo per day. |
| `buy` | one of `buy`/`sell` | Import price during the promo. Leave out to keep the normal import price. |
| `sell` | one of `buy`/`sell` | Export price during the promo. Leave out to keep the normal export price. |
| `label` | no | Name for the promo in Home Assistant, e.g. `Axle event`. |
| `replace_label` | no | Remove promos with the same label that haven't started yet, so the label always means one window. |
| `repeat` | no | `once` (default), `daily` or `weekly`. |
| `weekdays` | for weekly | e.g. `[sat, sun]`. |
| `until` | when repeating | Last day of a repeating promo. |
| `on_overlap` | no | `error` (default), `replace` (remove overlapping promos) or `allow` (keep both). |

Adding a promo that already exists does nothing, so it's safe for automations that
re-run. Prices are plain numbers in whatever units your Sigen tariff uses (the same numbers you'd
type in the Sigen app), and can't be negative.
If the Sigen cloud can't be reached, the save is retried for about 1.5 minutes before
the action fails.

```yaml
action: sigen_promo.add_promo
data:
  start: "{{ states('sensor.axle_energy_event_start') }}"
  end: "{{ states('sensor.axle_energy_event_end') }}"
  sell: 100
  label: Axle event
  replace_label: true
  on_overlap: replace
```

```yaml
action: sigen_promo.add_promo
data:
  start: "2026-11-07 00:00:00"
  end: "2026-11-07 24:00:00"
  buy: 7
  sell: 15
  repeat: weekly
  weekdays: [sat, sun]
  until: "2026-12-27"
```

### `sigen_promo.remove_promo`

Removes promos by `label`, by `start`–`end` window (overlapping promos), or both.
Promos that have already started are kept unless `include_started: true`.

### `sigen_promo.clear_promos`

Removes every promo.

### `sigen_promo.list_promos`

Returns every current and upcoming promo (response only).

## Example automation: Axle VPP events

Keeps one promo, labelled `Axle event`, in line with the next Axle event: added when
Axle announces it, moved if it changes, removed if it's cancelled (a promo that has
already started is left alone). It re-checks every 30 minutes in case a save failed,
and does nothing while the Axle integration is unavailable. EDF free electricity and
Sunday Saver work the same way with their start/end sensors and a `buy` price.

```yaml
alias: "Sigen promo: Axle VPP event"
mode: queued
max: 3
max_exceeded: silent
triggers:
  - trigger: state
    entity_id:
      - sensor.axle_energy_event_start
      - sensor.axle_energy_event_end
      - sensor.axle_energy_event_type
    not_to: unavailable
  - trigger: time_pattern
    minutes: "/30"
actions:
  - delay: { seconds: 10 }   # start, end and type update separately
  - condition: not            # an Axle outage isn't a cancellation
    conditions:
      - condition: state
        entity_id:
          - sensor.axle_energy_event_start
          - sensor.axle_energy_event_end
          - sensor.axle_energy_event_type
        state: unavailable
        match: any
  - variables:
      start: "{{ states('sensor.axle_energy_event_start') }}"
      end: "{{ states('sensor.axle_energy_event_end') }}"
      upcoming: "{{ as_datetime(start) is not none and as_datetime(end) is not none and as_datetime(end) > now() }}"
  - choose:
      - conditions:
          - "{{ upcoming }}"
          - condition: state
            entity_id: sensor.axle_energy_event_type
            state: export
        sequence:
          - action: sigen_promo.add_promo
            data:
              start: "{{ start }}"
              end: "{{ end }}"
              sell: 100
              label: Axle event
              replace_label: true
              on_overlap: replace
      - conditions:
          - "{{ upcoming }}"
          - condition: state
            entity_id: sensor.axle_energy_event_type
            state: import
        sequence:
          - action: sigen_promo.add_promo
            data:
              start: "{{ start }}"
              end: "{{ end }}"
              buy: 0
              label: Axle event
              replace_label: true
              on_overlap: replace
    default:
      - action: sigen_promo.remove_promo
        data:
          label: Axle event
```

## Good to know

- **If a save fails** after its retries, a Repairs issue appears under
  **Settings → System → Repairs**, and goes away after the next successful save.
- **Every save replaces all promos** on the station. The integration always reads the
  current list first and runs one change at a time, so automations can't wipe each
  other's promos. Changes made in the Sigen app at the same moment could still be lost.
- **Repeating promos need an end date.** Without one, the cloud stops listing them the
  day after they start, and the next change would wipe them.
- **Past dates are dropped** by the cloud. A window that has already started is saved
  from today.
- **Overlapping promos** are accepted by the cloud, but which one applies is unknown,
  so `add_promo` refuses them unless you set `on_overlap`.
- A promo you set in the Sigen app shows up within one refresh interval, labelled
  `Sigen app`. If you set a repeating promo in the app, give it an end date (see above).

## Command-line tool

[`tools/sigen_promo.py`](tools/README.md) does the same from a terminal, and its README
documents what the API does behind the scenes. It shares its promo logic with the
integration (`custom_components/sigen_promo/promo.py`), so run it from a full copy of
this repo.
