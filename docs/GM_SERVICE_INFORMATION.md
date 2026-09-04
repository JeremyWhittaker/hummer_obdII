# GM Service Information: the shopping list, not the shopping trip

Status: **a retrieval checklist. Nothing bought, nothing subscribed, nothing
tapped, nothing authorised.**

[CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md) states the rule that governs this
whole area:

> **Never connect anything to a pair inside the vehicle until GM service
> information or measured physical-layer evidence identifies that specific bus
> and its bitrate.**

That page is the decision document. This one is its companion and does one
narrower job: **it says exactly what to go and get, so that the identification
half of that rule could ever be satisfied.** It does not relax the rule, it does
not recommend a purchase, and satisfying it would still leave the connection
decision unmade — see [Non-goals](#non-goals), which is not boilerplate here.

Everything below is VIN-specific work on Jeremy's own vehicle, done privately,
reading documents GM sells to the public.

---

## 1. Why this comes before hardware

The instinct is backwards. A CAN interface is a purchase; service information is
a subscription; the interface feels like the bigger commitment and therefore the
later one. It is the other way round, and the reason is in the failure mode.

### What a wrong bitrate actually does

A CAN controller is configured for exactly one bit time. Put it on a segment
running at a different rate and it samples the line in the wrong places, so the
bits it decodes are not the bits on the wire. Two rules then fail almost
immediately. CAN inserts *a bit of opposite polarity after five consecutive bits
of the same polarity*, and *six consecutive bits of the same polarity are
considered an error*; and the frame it thinks it is reading fails its form and
CRC checks.

A controller in the error-active state answers a detected violation by
transmitting an **active error flag — six dominant bits**. Six dominant bits are
themselves a stuff violation for every other node on the segment, so they all
signal an error too, and the frame in progress is destroyed for everyone. The
real transmitter retries. The mismatched node objects again.

There is a self-limiting mechanism, and it is worth stating precisely because
it is the thing people reach for to argue this is survivable: *"When TEC or REC
is greater than 127 and less than 255, a Passive Error frame will be transmitted
on the bus... When TEC is greater than 255, then the node enters into Bus Off
state, where no frames will be transmitted."* So a conformant controller does
eventually take itself out of the argument.

**That is a mitigation, not an argument.** It arrives after a burst of destroyed
frames; the destroyed frames belong to whatever the segment actually carries;
and on a vehicle where propulsion, braking and steering share infrastructure
with everything else, "the standard says my mistake is self-limiting" is not a
sentence to say out loud about a live bus. Neither is "I will find out which bus
this is by joining it."

The same shape applies to a Classical/FD mismatch, which is the mismatch most
likely here. `CAN_FD_EXPANSION.md` puts it as: *an FD frame looks like a
protocol violation to a classical node, which duly reports it as an error.* One
honest complication: some classical controllers implement protocol-exception
handling and go quiet on an FD frame rather than objecting to it. **Which
behaviour a specific part has is a datasheet question, and finding out on the
vehicle is exactly the experiment this rule exists to forbid.**

### Two failures that have nothing to do with bitrate

Both are reasons the document comes first even if you somehow knew the rate.

* **Termination.** GM's own technician publication describes the VIP physical
  layer: *"Each CAN data network consists of two twisted wires, called CAN (+)
  and CAN (-), with a 120 ohm (Ω) termination resistor at each end of the bus
  between the CAN (+) and CAN (-) circuits."* Adding a third termination to an
  already-terminated segment halves the differential load and degrades every
  edge on it — a purely passive way to break a bus, available to anyone who
  connects a terminated interface to the middle of one. The schematic is what
  tells you where the ends are.
* **Which pair is it.** A scope can measure a bit time. It cannot tell you the
  bus's *name*, whether the point you found is a splice on a longer segment,
  what else lives on it, or whether it is safety-critical. Those are document
  facts. GM's Ethernet pairs make this concrete: they are also *"a single
  twisted copper pair"*, they carry *"100 Mbit/s and 1000 Mbit/s"*, and the
  *"Ethernet bus does not use terminating resistors."* Two twisted wires in a
  GM harness are not automatically CAN.

### The number we most need may not be in there

Be honest about this up front, because it changes what "success" looks like.
The GM TechLink article above describes the VIP physical layer in detail — CAN
pairs, 120 Ω at each end, LIN, Ethernet at 100 and 1000 Mbit/s, low-speed GMLAN
retired — and **names no CAN bitrate anywhere.** Whether GM Service Information
states nominal and data-phase bitrates per bus for this vehicle is **not
established by this document**. Ask for it explicitly, and if it is absent,
record the absence: a subscription that fails to produce a bitrate has still
produced a result, and the rule stays unsatisfied.

---

## 2. Where to get it legitimately

### The route

GM sells service information to non-dealers through **ACDelco TDS**
(`acdelcotds.com`), which is a storefront for three separable things:

| Product | What it is | Wanted here |
|---|---|---|
| **Service Information (SI)** | The factory documents: schematics, connector end views, component locations, descriptions and operation, DTC documents | **This is the whole ask.** |
| **GDS2 / Tech2Win** | The dealer diagnostic application | **No** — a bidirectional tool; already declined in [CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md) and [ACCESS_MATRIX.md](ACCESS_MATRIX.md) |
| **SPS / SPS2 (Service Programming System)** | Module programming and configuration | **Absolutely not** — see [Non-goals](#non-goals) |

They share one account. Buying the wrong line item is a two-click mistake, so
know which line you are buying before you open the page.

`oem1stop.com` is the neutral index of manufacturer service-information portals
— useful for confirming you are on a real OEM route rather than a reseller. It
publishes no prices and says each site *"may also require the purchase of
daily/weekly/monthly/annual subscriptions"*. In Canada, `oemrepairinfo.ca/gm`
points back to `acdelcotds.com` for TIS2Web subscriptions.

### Pricing: what was actually found, and what was not

**This section will go stale. Read the live price on the site.**

| Claim | Evidence | Confidence |
|---|---|---|
| SI has historically been sold in three durations: a few days, a month, a year | A 2017 product review recorded *"3 days for $20, 1 month for $150 and 1 year for $1200"* | The **shape** is well attested; the **figures are nine years old** |
| Prices rose on 1 July 2026 | Trade article dated 3 June 2026 reporting a NASTF notice: *"subscription prices for applicable ACDelco TDS offerings purchased on or after July 1 will increase"* | Reported; the article's own pricing table did not survive retrieval |
| The complete Techline Connect subscription price was unchanged in that revision | Same article | Reported |
| SPS per-VIN access dropped from 24 months to 30 days | Same article | Reported. Not our concern — noted because it shows the terms move |
| **The current SI price** | **Not established.** `acdelcotds.com/subscriptions` returns HTTP 403 to automated fetch and renders its price table client-side; the archived copy is a JavaScript shell. Search snippets offered "$22" and "$57" for a three-day; neither traced to a dated first-party page, so neither is quoted here as a fact | **Unknown** |

**The plan that survives the uncertainty:** the durable feature of the offering
is that *day-scale access exists and is by far the cheapest tier*. This
checklist is a bounded retrieval task — a list of documents to open, save the
facts from, and close. Buy the shortest tier that fits the list, and go in with
the list already written. That is why this page exists.

### What is not the route

* **Scraped mirrors.** Operation CHARM (`charm.li`) is free and real and covers
  roughly 1982–2013. This vehicle is far outside that. Other mirrors of GM SI
  exist; they are of unknown provenance and unknown currency, and on a question
  where being wrong means a damaged bus, "unknown currency" disqualifies a
  source by itself.
* **Forum pinouts.** `CAN_FD_EXPANSION.md` already rules these out by name:
  *not "until the connector pinout is posted on a forum."*
* **Printed factory manuals.** Helm Incorporated is GM's publisher of record for
  printed material. A catalogue query for the 2024 GMC Hummer EV across all
  categories returned five publications — Full Owner Manual, French Owner
  Manual, EOSI, French EOSI, and an EV warranty guide — **all owner-manual
  class, with no service manual and no wiring diagram book.** Treat a printed
  service manual for this vehicle as unavailable unless shown otherwise.
* **Aftermarket aggregators** (ALLDATA, Mitchell 1) license OEM data. Whether
  either carries Global B wiring for this VIN was **not established** and should
  not be assumed.
* **NASTF Vehicle Security Professional credentials** are for immobilizer, key
  and secure-access functions. Nothing on this checklist is in that category. A
  page demanding VSP registration is a page for something this project does not
  do.

---

## 3. The retrieval checklist

VIN-specific. Pull each document **for this VIN**, not for "a Hummer EV" — see
[RPO codes](#5-rpo-codes-and-why-the-exact-configuration-decides-which-schematic-is-right).

Document-name conventions vary across GM SI generations. Names in **bold** are
attested in sources cited at the end of this section; names in *italics* are
the category to search for and may be titled differently in this vehicle's SI.

| # | What to retrieve | What it tells you | What this project would do with it |
|---|---|---|---|
| 1 | **Data Link References** | GM's instruction to technicians is that this document *"lists the control modules and the buses with which the modules communicate"* — a module-to-bus map, with optional modules flagged | **The single highest-value item.** This project has eight addresses the truck named for itself and **no idea which bus any of them is on**. It would place `45`, `40`, `CB`, `CD`, `17`/`1D`/`1E` and `28` onto named segments in one document, and would give the first structural explanation of why `28` and `40` split on CAN priority ([CAN_PRIORITY.md](CAN_PRIORITY.md) calls that split *suggestive of a network boundary* while measuring none) |
| 2 | *Data Communication — Description and Operation* | The architecture in prose: how many buses, what each is for, what the gateway isolates from what | Converts a behavioural finding into an architectural one. [PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md) concluded "the gateway is a boundary" from **zero bytes in 30.1 seconds** of receive-only monitoring. That is evidence about the DLC; this is the design |
| 3 | *Data Communication / Serial Data Schematics* | The pairs themselves: circuit numbers, wire colours, splice points, terminating resistor locations, connector references, which module sits where on each segment | The identification half of the hard rule. Also the termination map, which matters independent of bitrate (§1) |
| 4 | *Bus bitrates — nominal and data phase, per bus* | The number the rule names | **Ask explicitly. Expect to be disappointed.** GM's public technician material describes the physical layer and states no CAN bitrate. If SI does not state it either, that is a finding worth writing down, and the rule remains unsatisfied by documents alone |
| 5 | Gateway module: component page, **connector end views**, pin functions | Which buses it bridges, which it isolates, its own connectors and power/ground | Module `45` names itself *"Gateway Module - GWM"*, answers services 01 and 09, and returned `7F 22 31` to all four ISO 14229-1 identification identifiers — the only things ever asked of it. SI would say what it is and what it borders. **Also a naming reconciliation:** GM's VIP material names a *"K56 Serial Data Gateway Module"*; whether this truck's `45` is that component is unestablished |
| 6 | BCM: component page, **connector end views**, and the *Power Moding* description | Body-domain wiring, and the power-mode state machine | GM states *"the K9 Body Control Module (BCM) is the Power Mode Master (PMM) and the K56 Serial Data Gateway Module is the back-up PMM. There are five power modes: Off, Accessory, Run/Service Mode (Engine Off), Propulsion (Engine On), and Start."* Module `40` answers nine identifiers here, only at priority `0x18`. Named power modes would give the recorder a vocabulary for vehicle state it currently infers from voltage |
| 7 | Battery-side modules: component pages, **connector end views**, locations | How many battery-side control modules this vehicle has, what each is called, and where each physically is | **This is the `CD` question.** The truck names *two* modules `BSM-BatterySysMngr`, at `CB` and `CD`. `CB` answers thirteen identifiers; `CD` refused all seventeen ever put to it, at both priorities, including the ISO standard identification set. GM's own bulletins covering *HUMMER EV Pickup 2022–2025* name **K16 Battery Energy Control Module** — one name, and we see two modules. SI is where that reconciles |
| 8 | Telematics / OnStar module: component page, bus membership, **connector end views** | Which bus the telematics module is on, and its designator | **No telematics module appears in this vehicle's eight-module census** — and that census is the set of addresses that answered service 09 PID `0A`, which is a statement about what answered, *not* proof of absence. Item 1 would settle whether such a module exists on this build and whether it is simply on a segment the gateway does not bridge to the DLC. Third-party SI extracts use *"K73 Telematics Communication Interface Control Module"*; **unverified for this vehicle** |
| 9 | **Connector end views** and connector / terminal part numbers, for every connector named above | Cavity layout, terminal type, part numbers | The item most likely to be mistaken for permission. Its legitimate use here is **identification and planning**, and the ability to say precisely which connector a future decision would concern. Retrieving it authorises nothing (§4) |
| 10 | *Component locations* and *harness routing views* | Where the modules and connectors physically are | Turns a schematic into something you could point at. Also the only way to know what a candidate access point is next to, which is a safety question on a vehicle with a 400 V pack |
| 11 | *Power distribution* and *ground distribution* schematics; fuse block details; the DLC's own feed | Which fuse and circuit feed the diagnostic connector, whether any nearby source is ignition-switched or retained-accessory timed, and how long RAP holds | **Immediately useful, and it touches no bus.** Verified here: the OBD-II port is *always live* (two independent observations), so the Pi and adapter are a permanent parasitic load; a 6.8-hour sleep trace exists with the rail settling at 12.80 V and **no parasitic-current figure**. Unattended autostart is gated on exactly this — [SAFETY.md](SAFETY.md) requires *"a verified vehicle sleep/wake cycle with acceptable 12 V draw, or confirmed ignition-switched power for the Pi."* SI naming a switched or RAP-timed source would open that gate by design instead of by endurance testing |
| 12 | DTC documents for the U-codes (Lost Communication With…) | Per-code: which module, which bus, and the diagnostic path | **The cheapest topology source in the whole list, and it requires touching nothing.** A "Lost Communication With X" document names the bus X is on. Dozens of them together reconstruct much of item 1. Note this vehicle currently stores **no DTCs at all** — the codes' *descriptions* are what is wanted, not our (empty) code list |
| 13 | The RPO list for this VIN | The exact build | §5. Needed to be sure items 1–12 are the right ones |

### Which document names above are attested

* **Data Link References**, the **K56 Serial Data Gateway Module**, the **K9
  Body Control Module** as Power Mode Master, the five power modes, and the CAN
  and Ethernet physical-layer descriptions are quoted from GM's own technician
  publication, *"Programming with the Vehicle Intelligence Platform"*, GM
  TechLink, Mid-March 2021.
* **K16 Battery Energy Control Module** is quoted from GM service bulletins
  whose model tables include *GMC HUMMER EV / HUMMER EV Pickup 2022–2025* and
  *HUMMER EV SUV 2024–2025* (PIP6009B, 12/16/2024; 24-NA-015, February 2025).
* **Connector End View** as an SI document class, and *"bus schematics and RPO
  codes will provide some clues"* for module placement, come from
  practitioner sources rather than GM directly.
* Everything in *italics* is a category, not a verified title.

---

## 4. What this document deliberately does not contain

For this vehicle, **none of the following is established anywhere in this
repository, and none of it is guessed here**:

* connector part numbers, terminal part numbers, or cavity/pin numbers;
* circuit numbers or wire colours;
* the names of the internal buses, how many there are, or which modules sit on
  each;
* any bus bitrate, nominal or data phase;
* whether any internal segment is CAN FD, Classical CAN, LIN or Ethernet;
* where terminating resistors are;
* whether the DLC itself negotiates CAN FD — still open, and still the one
  question hardware could answer cheaply at a connector we are already
  permitted to use ([CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md), step 2).

That list *is the document's content*. If a future edit fills any line in from
memory, a forum, or a plausible-looking parts diagram, the edit is wrong. These
get filled in from SI against this VIN, or they stay empty.

---

## 5. RPO codes, and why the exact configuration decides which schematic is right

### What they are

A Regular Production Option is *"a standardized three-character alphanumeric
code used by General Motors to designate vehicle options and modifications"*.
The format is *"typically @##, but @@# also occurs, along with other more rare
exceptions, including four-character codes."* Collectively they are the
vehicle's as-built configuration.

### Why the exact configuration matters here

Not for trim-level trivia. Because **the module-to-bus map is
RPO-conditional**, and GM says so itself. Its instruction to technicians is to
refer to *"the Data Link References that lists the control modules and the buses
with which the modules communicate in the appropriate Service Information **and
the vehicle build RPO codes to determine optional control modules**."*

Read what that means for this checklist. Item 1 — the highest-value document in
the list — is not a single document for "a Hummer EV". It is a document that has
to be read *against this truck's option content*, because an optional module is
present or absent depending on RPO, and an absent module's bus does not exist on
this vehicle. Practitioners give the same advice from the other direction:
*"bus schematics and RPO codes will provide some clues"* as to which module sits
where.

Two ways that bites:

* **A schematic pulled for the wrong configuration can show a pair this truck
  does not have**, or omit one it does. On a topology question, that is the
  difference between the right pair and the wrong pair — which is the difference
  §1 is about.
* **Body style alone is a different vehicle in GM's own records.** GM's
  bulletins list *HUMMER EV Pickup* and *HUMMER EV SUV* as separate model lines
  with different model-year ranges. "Hummer EV" is not a specification.

### Where they are found

In descending order of trustworthiness:

1. **GM Service Information itself, entered by VIN.** SI is VIN-driven; the
   build list it resolves to is the authoritative one, and it is inside the
   subscription you are already buying. **This is the answer.** The rest of this
   list is for cross-checking it.
2. **The dealer**, via build data by VIN.
3. **On the vehicle — but look before assuming.** The Service Parts
   Identification (SPID) label historically carried the RPO list and is *"most
   often located on the back of the glovebox door, on the inside of the trunk
   lid, or on the bottom of the spare tire cover"*; the same source states that
   in 2017 *"the SPID was replaced by a QR code label located on the B-pillar
   (driver's side, between front and rear doors)."* **Neither has been checked
   on this truck.** Nobody has looked. The right move is to look, photograph
   what is actually there, and record it — not to repeat either claim as though
   it were a fact about this vehicle.
4. **Free VIN-to-RPO decoder sites.** Unverified provenance. Fine for a sanity
   check against (1); never a substitute for it.

Whatever the source, keep the RPO list **out of the public repository** — it is
a fingerprint of one specific vehicle, and [SAFETY.md](SAFETY.md)'s publication
rules already exclude identity-bearing artefacts.

---

## Non-goals

This section is load-bearing. It is the reason the rest of the page is safe to
write down.

**This is not authorisation to tap anything.** Retrieving a pinout does not
create permission to use it. The rule in
[CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md) has two halves — *identify the bus
and its bitrate*, and then a decision — and this checklist addresses only the
first. Completing every row would leave the connection decision exactly where it
is now: unmade, and not made here. The repository has never authorised a
physical tap; [ACCESS_MATRIX.md](ACCESS_MATRIX.md) records the vehicle's
internal networks as needing *"a physical tap, which needs the hardware and the
identification in CAN_FD_EXPANSION.md, and is explicitly not authorised by this
repository."* Nothing on this page changes that sentence.

**This is not a path to control.** Twenty-two UDS services are permanently
forbidden — `04`, `08`, `10`, `11`, `14`, `27`, `28`, `2E`, `2F`, `31`, `34`,
`35`, `36`, `37`, `38`, `3B`, `3D`, `3E`, `83`, `84`, `85`, `87` — refused by
all five gates, with an import-time assertion that fails the build if one is
added to the allowed set. Better wiring knowledge does not make a write service
readable. Verify with:

```bash
PYTHONPATH=src python3 -c "from hummer_obd import safety; print(len(safety.FORBIDDEN_SERVICES), sorted(safety.FORBIDDEN_SERVICES))"
PYTHONPATH=src python3 -m hummer_obd.access --check
```

**SPS is out of scope entirely.** Module programming, calibration, VIN writing,
configuration, and gateway learn procedures are not part of this project in any
form, at any time, under any circumstances. Note the trap: **the same TDS
account that sells Service Information also sells SPS**, and SPS is sold
*per VIN* — so the checkout page will ask for this vehicle's VIN, which makes it
feel like the VIN-specific thing you came for. It is not. Buy SI. If a page
wants a VIN in order to grant programming access, close it.

**This is not GDS2 or MDI2.** Already decided, twice, and for the same reason:
*a bidirectional tool whose entire value is that it can command the vehicle.*
Introducing one would not extend this project; it would replace a structural
safety model with "be careful".

**This is not a licence to redistribute.** GM SI is licensed content. Facts
learned from it may be recorded in this repository **in the project's own
words**, attributed to SI, and used to correct what is written here. Exported
pages, screenshots and PDFs do not go in the repository, and neither does the
VIN or the RPO list.

**This is not a substitute for measurement.** SI describes a design; the truck
is the truck. This project has already contradicted two published identifier
labels using its own data, and inferred the wrong gateway address from real
observed behaviour. A document is evidence, and it is a kind that can be stale,
configuration-mismatched, or simply about a different build.

---

## What could not be established while writing this

Recorded so that the next person does not repeat the search:

* **The current price of a GM SI subscription at any tier.**
  `acdelcotds.com/subscriptions` refuses automated fetch (HTTP 403) and renders
  prices client-side, so archived copies are empty shells. The 2017 figures and
  the June 2026 "prices increase 1 July" notice are the strongest datable
  evidence found. Read the live page in a browser.
* **Whether GM SI states CAN bitrates for this vehicle at all.** GM's own public
  technician material describes the VIP physical layer and gives no CAN rate.
  This is the pivotal unknown in the entire document.
* **Whether SI uses "K56", "K9", "K16", "K73" for *this* vehicle's modules.**
  K56/K9 are attested for VIP generally; K16 is attested in bulletins whose
  model tables include this vehicle; K73 came from a practitioner source and is
  unverified here. None of them has been tied to a diagnostic address on this
  truck.
* **Why the vehicle's own census contains no telematics module**, and whether
  that is absence, a different address, or a module that does not answer service
  09 PID `0A`.
* **Where the RPO label is on this specific truck**, or whether it has one.
  Nobody has looked.
* **Whether ALLDATA or Mitchell 1 carry Global B wiring for this VIN.**
* **Whether the diagnostic connector negotiates CAN FD.** Unchanged, and still
  the one question hardware answers cheaply at a connector we are already
  permitted to use.

---

## Sources

Primary (GM-authored):

* *Programming with the Vehicle Intelligence Platform*, GM TechLink, Mid-March
  2021 — <https://gm-techlink.com/wp-content/uploads/2021/04/GM_TechLink_06_Mid-March_2021.pdf>
* GM Preliminary Information **PIP6009B**, 12/16/2024 (model table includes
  *GMC HUMMER EV 2022–2025*, *HUMMER EV SUV 2024–2025*; names *K16 Battery
  Energy Control Module*) — <https://static.nhtsa.gov/odi/tsbs/2024/MC-11012017-0001.pdf>
* GM Service Bulletin **24-NA-015**, February 2025 (model table includes
  *HUMMER EV Pickup 2022–2025*) — <https://static.nhtsa.gov/odi/tsbs/2025/MC-11015213-0001.pdf>

Subscription and availability:

* ACDelco TDS subscriptions page — <https://www.acdelcotds.com/subscriptions> (HTTP 403 to fetch; prices client-side)
* *ACDelco TDS subscription changes take effect July 1*, 3 June 2026 — <https://www.indiegarage.ca/acdelco-tds-subscription-changes-take-effect-july-1/>
* Product review with 2017 SI pricing — <https://www.cadillacvnet.com/product-reviews/tools-shop-aids/product-review-gm-service-information-from-acdelcos-technical-delivery-system/>
* OEM1Stop — <https://www.oem1stop.com/>
* OEM Repair Info (Canada), GM entry — <https://www.oemrepairinfo.ca/gm/>
* Helm Incorporated, 2024 GMC Hummer EV catalogue query (owner-manual class only) — <https://www.helminc.com/helm/Result.asp?Mfg=GMC&Make=GMC&Model=HEV&Year=2024&Category=&selected_media=ALL>

Reference:

* CAN error handling, stuff rule and error-counter thresholds — <https://en.wikipedia.org/wiki/CAN_bus>
* Regular Production Option and the SPID label — <https://en.wikipedia.org/wiki/Regular_Production_Option>
* Gateway-network diagnosis, *Data Link References* in practice — <https://diag.net/msg/m4gnxx6lq24xdh47yaow4odaix>

In-repository, for the vehicle-side facts quoted above:
[CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md),
[ACCESS_MATRIX.md](ACCESS_MATRIX.md),
[CAN_PRIORITY.md](CAN_PRIORITY.md),
[GM_MODULE_MAP.md](GM_MODULE_MAP.md),
[PASSIVE_CAN_VALIDATION.md](PASSIVE_CAN_VALIDATION.md),
[SAFETY.md](SAFETY.md),
[VALIDATION.md](VALIDATION.md).
