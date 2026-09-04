# Using GM's own tool as a truth oracle

**Status: proposal. Nothing bought, nothing installed, no session run.** No
claim on this page rests on a GDS2 screen anyone here has seen. Everything about
this project's own data is verified with a command that is quoted beside it;
everything about GM's tooling is cited, and where a citation is a forum post or
a reseller rather than GM, it says so.

This page exists for the same reason [CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md)
does: so that if the question comes up under time pressure — a subscription
bought on impulse, a friendly shop with an hour free, a charge session worth
catching — the comparison is already done and the boundary is already written.

---

## 1. Why this is worth a day

The recorder writes **53 columns** and the gate holds **35 identifiers**. Only
some of those identifiers mean anything yet:

```bash
PYTHONPATH=src python3 -c "
from collections import Counter
from hummer_obd.confidence import CONFIDENCE, LEVEL_NAMES
c=Counter(e.level for e in CONFIDENCE.values())
print(len(CONFIDENCE), 'identifiers:', {LEVEL_NAMES[k]: c[k] for k in sorted(c)})
"
# 35 identifiers: {'sourced only': 4, 'answers here': 17, 'decoded': 5,
#                  'cross-validated': 4, 'cross-validated in more than one state': 5}
```

**Seventeen identifiers are at level 1.** The vehicle answers every one of them,
the bytes land in a CSV every cycle, and this project claims nothing about what
they are. Two of them are worse than unknown: `0x5401` and `0x2429` are
identifiers whose *published label this vehicle contradicted*, so their sources
are not merely silent but wrong.

The obvious way to fix that is more sourcing, and on 2026-09-04 that route was
run to exhaustion and written up in
[SOURCING_2026-09-04.md](SOURCING_2026-09-04.md). Two independent sweeps across
eight OBDb repositories, `meatpiHQ/wican-fw` profiles, issues and pull requests,
and `commaai/opendbc` added **zero** identifiers. That document's own conclusion
is the premise of this one:

> The correct next move is not a wider search; it is to stop expecting one to
> work.

GM's tool does not have this problem. It is the tool the vehicle was designed to
be read by, and it displays these quantities with names and units on the screen.
One afternoon of lining up a GDS2 parameter name against a byte-exact recording
taken in the same vehicle state could move several level-1 fields to level 2 or
3 — which is more than either sourcing sweep produced.

### The one thing it will not do

**GDS2 does not hand us new identifiers.** It shows a *parameter*, named and
scaled, on a *module*. It does not tell us which service-22 identifier carries
it, and this project adds an identifier only when a fetchable source names it
exactly ([SAFETY.md](SAFETY.md), change-control rules). A GDS2 screenshot is not
that source.

So the value proposition is precise and narrow: **it promotes fields we already
read.** It does not widen what the node transmits, and no part of this workflow
touches the gate. The only route by which GDS2 could yield an identifier is by
capturing its own bus traffic, which is [option D](#option-d--splitter-plus-the-passive-monitor-capturing-gds2s-own-traffic)
below and is declined.

There is a second, cheaper category of win worth naming, because it costs
nothing extra once the tool is connected: several rows in
[ACCESS_MATRIX.md §3](ACCESS_MATRIX.md#3-what-cannot-be-reached) say
**unsourced**, meaning *no public source names an identifier*. GDS2 would settle
whether the quantity **exists on this vehicle at all** — a different question,
and one this project has never been able to ask. Motor RPM, torque, inverter
temperature and a regen power limit are all in that category. Either answer is
worth having: if GDS2 shows them, the sourcing dead end is a documentation gap;
if GDS2 does not, it is a real absence.

---

## 2. The four things people mean by "GM's tool"

They are routinely spoken of as one thing. They are four, they do different
jobs, and the distinction is the whole safety argument of this page.

| | What it is | Used here? |
|---|---|---|
| **MDI 2** | The hardware. GM's own user guide: *"The MDI 2 Vehicle Communication Interface kit contains an external test equipment (ETE) cable that connects the MDI 2 to the vehicle's SAE J1962 Data Link Connector (DLC)"*, it *"is capable of communicating over a USB cable, Ethernet cable, or wireless network (WLAN)"*, and it *"is a SAE J2534 device… for pass-thru programming of the vehicle's ECUs"*. It holds no diagnostic content of its own. Note also *"The MDI 2 will be powered by the vehicle's 12-volt battery"* — pin 16, the same supply the MX+ dongle takes. | **Yes** — as the interface, physically, in a human's hand |
| **Techline Connect** | The Windows launcher and platform that ties GDS2, SPS2 and Tech2Win together and handles vehicle selection and the session against GM's servers. *(Described by resellers and trade press; no GM document verifying this was retrievable.)* | **Yes** — it is how GDS2 is launched |
| **GDS2** | Global Diagnostic System 2. The **diagnostic** application: module scan, DTC read/clear, Data Display live data, Identification Information, and control/configuration functions. Applies to "select 2010 to 2013 model year and all 2014 and newer GM vehicles". | **Yes, and only two of its features** — Data Display and Identification Information |
| **SPS / SPS2** | Service Programming System. The **programming** application: module flashing, calibration, configuration, setup, and vehicle-specific service routines. | **NEVER.** Not once, not "just to look", not for the battery data retrieval described below |

A fifth name appears in the same bundles — **Tech2Win**, a software emulation of
the legacy Tech 2 scan tool for older Global A vehicles. It is irrelevant to a
2024 BT1 truck and is mentioned only so nobody wonders why it is missing.

### The trap in the middle of GM's own Ultium documentation

This is the single most important paragraph on this page, and it is not
hypothetical.

GM's preliminary information bulletin **PIP6009B** (published 12/16/2024),
which covers `GMC HUMMER EV 2022 - 2025` and `GMC HUMMER EV SUV 2024 - 2025`
among other Ultium vehicles, tells a technician facing Ultium pack fault codes
to collect battery data. The procedure it gives is:

> **SPS2 Hybrid/EV Battery Data Retrieval**
>
> 1. Access the Service Programming System (SPS) and follow the on-screen instructions.
> 2. Perform the following SPS Programming function:
> 3. K16 Battery Energy Control Module and follow the on-screen instructions.
> 4. Perform the following SPS Function:
>    4.1 Hybrid/EV Battery Data Retrieval.

So the deepest battery data extraction GM documents for an Ultium pack — very
plausibly including the per-cell detail this project most wants and
[cannot source](ACCESS_MATRIX.md#3-what-cannot-be-reached) — lives inside
**SPS2**, described by GM as an *SPS Programming function*, invoked from the
programming system, on the module whose calibration SPS2 exists to rewrite.

It is the most tempting thing in GM's entire Ultium toolkit and it is on the
wrong side of this project's line. It is not used. It is not attempted. It is
not "read-only really, because it is called retrieval". The bulletin's own
adjacent instruction is *"DO NOT clear the codes or program the BECM"*, which is
a good indication of the neighbourhood that menu lives in.

**GDS2 Data Display only. That is the whole permitted surface.**

---

## 3. Getting in legitimately

The licensed route for a non-dealer in the US is **ACDelco TDS**
(`acdelcotds.com`) — GM's own channel for selling Service Information,
diagnostics and programming access to independent shops and owners. What is sold
is a subscription to **Techline Connect**, which bundles GDS2, SPS2 and online GM
Service Information.

> **Read [GM_SERVICE_INFORMATION.md §2](GM_SERVICE_INFORMATION.md#2-where-to-get-it-legitimately)
> alongside this.** That page covers the same storefront in more detail for a
> different purpose — buying Service Information documents to identify the
> internal buses — and its pricing evidence is better sourced than mine. Two
> things to notice when reading the two together. First, they agree on the
> facts: the same 1 July 2026 price increase, the same SPS per-VIN change from
> 24 months to 30 days, the same failure to get a current price out of
> `acdelcotds.com`. Second, **that page marks GDS2 "No"** — correctly, *for its
> question*, which is about wiring diagrams and where a bidirectional tool buys
> nothing. This page proposes GDS2 for a narrower one: read-only live data, on a
> separate machine, with the boundary in [section 8](#8-the-hard-boundary).
> The two conclusions are about different jobs, and neither licenses the other.

**What I could not verify, and will not guess.** `acdelcotds.com`,
`gmparts.com` and GM's own `gsitlc.ext.gm.com` all returned **HTTP 403** to
every fetch attempt made while writing this page. **No price on this page comes
from GM.** Third-party and forum figures encountered ranged from a `$45`/year
reference and a `$57` three-day subscription through `~$400`/year to a reseller
listing at `$1,800`; they are mutually inconsistent, several are visibly stale,
and none should be planned against. **Get the number from `acdelcotds.com`
directly, on the day.**

What can be said with a source, all of it subject to change:

* **Subscriptions come in short and long durations.** Short terms (1–3 days) are
  described as non-refundable; longer terms (1 month – 1 year) refundable at
  GM's discretion. *(Third-party summary, not GM.)*
* **Terms changed on 1 July 2026.** Prices for applicable ACDelco TDS offerings
  purchased on or after that date increased, and SPS per-VIN purchases went from
  granting programming access for **24 months** to **30 days**. The Techline
  Connect subscription price itself was reported unchanged.
  *([Indie Garage](https://www.indiegarage.ca/acdelco-tds-subscription-changes-take-effect-july-1/),
  a trade publication, not GM.)* This matters here only as evidence that the
  terms move; it does not affect a diagnostics-only workflow, which never buys
  SPS access.
* **Some vendors sell GDS2 diagnostics without programming.** At least one
  reseller states plainly that "GDS2 subscriptions are strictly for diagnostics.
  They do not include module programming capabilities." If a diagnostics-only
  subscription exists at the price point, **it is the right one to buy** — not
  as an economy, but because a subscription that structurally cannot program is
  a better boundary than a promise not to.
* **Hardware: an SAE J2534 pass-through interface is required.** The
  **MDI 2 (EL-52100-AM / -AM2)** is GM's own; a reseller listing describes it as
  the one "Verified supported" and says other J2534 devices need confirmation
  before purchase. Whether a third-party J2534 device works for GDS2 on a 2024
  Global B vehicle is **not established here**.

**On the things that showed up in the search results and are not access
routes.** Cracked GDS2 builds, "mega database update packs", and clone
interfaces sold to run them are widely advertised. They are not covered by this
document and are not to be used. The point of the exercise is a *trustworthy*
reading; a tool of unknown provenance running an unknown database against this
truck fails on both counts at once.

---

## 4. The crux: one connector, two tools

The J1962 connector has one socket, and both tools are built on the assumption
of being the only thing in it. This project's OBDLink MX+ is a **dongle that
sits in that connector** and reaches the Pi over Bluetooth SPP
([ARCHITECTURE.md](ARCHITECTURE.md)). The MDI 2 reaches the same connector by
its own DLC cable. **They want the same socket.**

Everything else on this page is downstream of that sentence, so here are the
actual options, with what each costs.

### Option A — alternate sessions

Record with the OBD node. Swap the dongle for the MDI 2. Read GDS2. Swap back.

Works for any quantity that does not move meaningfully across the swap. Fails
for anything that moves in seconds.

Which of our targets survive a swap is a measured question, not a guess:

```bash
PYTHONPATH=src python3 -c "
import csv, glob, collections, sys
cols=sys.argv[1:]
v=collections.defaultdict(list)
for f in sorted(glob.glob('evidence/sessions/drive-*.csv')):
    for r in csv.DictReader(open(f)):
        for c in cols:
            if r.get(c): v[c].append(int(r[c],16))
for c in cols:
    s=v[c]; d=collections.Counter(s)
    print(f'{c:22s} n={len(s):5d} {min(s):6d}..{max(s):6d} distinct={len(d):4d}')
" evse_current_raw coolant_1_raw coolant_2_raw hv_temp_raw compressor_temp_raw
# evse_current_raw       n= 1570     36..   389 distinct=   8
# coolant_1_raw          n= 1571    905..  1250 distinct=  63
# coolant_2_raw          n= 1571    413..   819 distinct= 165
# hv_temp_raw            n= 1571      0..   100 distinct=   7
# compressor_temp_raw    n= 1155    107..   119 distinct=  13
```

Eight distinct values in 1570 samples of `0x4149`, thirteen in 1155 of `0x2709`:
these are fields that hold. A swap taking minutes does not threaten them.

`pack_a`, by contrast, is hopeless under alternation — but `pack_a` is already
level 4 and needs nothing from this exercise:

```bash
PYTHONPATH=src python3 -c "
import csv,glob
v=[r['pack_a'] for f in sorted(glob.glob('evidence/sessions/drive-*.csv'))
   for r in csv.DictReader(open(f)) if r.get('pack_a')]
print('pack_a n=',len(v),'distinct=',len(set(v)))
"
# pack_a n= 3241 distinct= 578
```

**The targets that matter are the slow ones. That is a happy accident and it is
what makes this workflow viable at all.**

### Option B — bracketed alternation *(recommended)*

Option A, with the OBD capture taken **both before and after** the GDS2 read,
in the same held vehicle state.

If a raw field reads the same in both brackets, the GDS2 value may be paired
with it, and the pairing is *measured* rather than hoped for. If it moved, the
pair is **discarded** — not interpolated, not averaged, discarded.

This costs one extra capture and converts the whole method from an assumption
into an observation. It is the protocol in [section 5](#5-the-protocol).

### Option C — Y-splitter, both tools connected

A J1962 splitter puts both tools in parallel on the same pins. Attractive
because it removes the swap. Not recommended, for reasons that stack:

* **Physical layer.** The DLC is specified for one tool. A splitter plus two
  cables adds stub length and capacitance to a CAN pair that has no obligation
  to tolerate it.
* **Two requesters is the real problem.** Two nodes both acknowledging is
  ordinary CAN; two nodes both *polling* is not. A powered ELM-class dongle is a
  normal bus node that asserts the acknowledgement bit on frames it hears — which
  is exactly why `hummer-obd-passive` has a receive-only mode (`STCMM0`) at all
  ([ACCESS_MATRIX.md §1](ACCESS_MATRIX.md#1-what-may-be-transmitted)).
* **The unknown that decides it.** Whether the MDI 2 negotiates **CAN FD** on
  this vehicle's diagnostic pair is not known to this project. If it does, a
  classical controller sharing the pair sees protocol violations and signals
  them. [CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md) states the corollary
  explicitly: *"A listen-only or receive-only mode does not make the wrong
  bitrate safe."*

The MX+ talks classical CAN to this truck's DLC successfully today, so the
diagnostic pair certainly carries classical CAN for *our* requests. That is not
the same statement as "GDS2 will not switch it", and this project does not have
the instrument that would tell the difference.

### Option D — splitter plus the passive monitor, capturing GDS2's own traffic

This is the version that would settle everything at once, so it is named rather
than quietly omitted. Put the MX+ on a splitter in receive-only monitor mode and
record the actual request/response frames GDS2 exchanges. That ties a GDS2
parameter name to a service-22 identifier **byte-exactly, with no scaling
inference at all** — and it transmits nothing new. The passive tool's whole
transmit set is ten adapter-configuration commands ending in `STCMM0`, plus
`STMA` to start the stream, and `monitor.assert_no_vehicle_traffic` holds at
import that none of it reaches the CAN bus:

```bash
PYTHONPATH=src python3 -c "
from hummer_obd.monitor import MONITOR_COMMANDS
from hummer_obd.safety import MONITOR_STREAM_COMMAND
print(MONITOR_COMMANDS); print(MONITOR_STREAM_COMMAND)"
# ('ATZ', 'ATE0', 'ATL0', 'ATS1', 'ATH1', 'ATAL', 'ATSP7', 'ATCAF0', 'ATCS', 'STCMM0')
# STMA
```

**It is still declined, on three independent grounds, any one of which is
sufficient:**

1. **The same CAN FD unknown as option C.**
2. **The capture is lossy by construction.** ASCII over Bluetooth RFCOMM at
   115200 baud against a bus carrying thousands of frames per second is recorded
   in [ACCESS_MATRIX.md](ACCESS_MATRIX.md#3-what-cannot-be-reached) as a hardware
   ceiling, and the loss is not itself recorded anywhere. A partial ISO-TP
   reassembly is exactly the failure that silently truncated a cell-voltage read
   on this recorder's first live run.
3. **It is a different kind of artefact.** Recording GM's own diagnostic exchange
   is not the same activity as recording this vehicle's answers to our own
   requests, and it deserves its own decision rather than arriving as a side
   effect of a decode session.

What would change it: a properly isolated CAN interface with hardware
timestamping, which is [CAN_FD_EXPANSION.md](CAN_FD_EXPANSION.md)'s territory,
and the hard rule there applies unchanged.

### Option E — do not correlate at all

Worth stating because it is easy to overlook while designing a protocol. For
several targets we do not need a paired numeric at all — only a screenshot
telling us **what quantity exists**.

If GDS2's thermal display shows twenty-four battery module temperatures while
`0x2AF1` returns twenty-four values, the array's *identity* is established even
if its scaling is not. That moves a line in
[GM_ENHANCED_CANDIDATES.md](GM_ENHANCED_CANDIDATES.md) from "24 values, meaning
unknown" to "24 module temperatures, scaling open" without any clock
synchronisation whatsoever.

This does **not** move a confidence level — level 1 means "answers here, meaning
not claimed", and a name is not a scaling — but it does change what we know, and
it is free.

---

## 5. The protocol

Two machines, one human, one vehicle, one held state. The numbering is the
running order and skipping a step has a cost noted beside it.

### Phase 0 — before the vehicle

1. **Provision a separate Windows machine.** It is not the Pi, it does not talk
   to the Pi, and it is not on the node's service path. The only thing shared
   between the two systems is the truck and the operator.
2. **Install and prove Techline Connect / GDS2 / MDI 2 firmware on the bench**,
   away from the vehicle. A first vehicle session spent on a software install is
   a session spent with the recorder stopped and nothing recorded.
3. **Write the target list down first** ([section 6](#6-what-to-point-it-at-first)).
   An open-ended browse produces screenshots nobody can pair with anything.
4. **Pull the sessions off the node. DATA FIRST.** Per
   [ACCESS_MATRIX.md §6](ACCESS_MATRIX.md#6-verifying-against-the-actual-vehicle):
   a session lost to a probe is not recoverable, and a probe deferred by ten
   minutes costs nothing.
   ```bash
   rsync -a <node>:~/hummer-obd/evidence/sessions/ evidence/sessions/
   ```
5. **Choose and write down the vehicle state to hold**, and hold exactly one:
   parked and awake, or plugged in and charging at a steady rate. The charging
   state is worth more — it is the only one that can decide `0x4149` and
   `0x5401` — and it is also the one most likely to change under you, so bracket
   it hard.
6. **Measure the clock offset between the Pi and the Windows machine.** Measured,
   from both clocks, written down. Not assumed, and not "they both use NTP".

### Phase 1 — first OBD bracket

7. Confirm the recorder is alive and rows are landing. `active (running)` is not
   evidence: this recorder once sat active for two hours writing blank rows.
   ```bash
   ssh <node> 'systemctl is-active hummer-drive; journalctl -u hummer-drive -n 3 --no-pager'
   ```
8. **Record the DTC baseline you will compare against afterwards**, addressed to
   module `45` — silence is not a reading:
   ```bash
   ssh <node> 'cd ~/hummer-obd && PYTHONPATH=src python3 -m hummer_obd.probe --commands 03 07 0A'
   ```
9. **Let it record at least ten cycles in the held state.** The measured median
   cycle across the committed corpus is **6.71 s** (p10 2.66, p90 9.37, over
   4876 gaps), so ten cycles is about seventy seconds:
   ```bash
   PYTHONPATH=src python3 -c "
   import csv, glob, statistics
   d=[]
   for f in sorted(glob.glob('evidence/sessions/drive-*.csv')):
       p=None
       for r in csv.DictReader(open(f)):
           try: e=float(r['elapsed_s'])
           except (ValueError,KeyError,TypeError): continue
           if p is not None and e>p: d.append(e-p)
           p=e
   d=sorted(x for x in d if x<120)
   print(len(d),'gaps; median', round(statistics.median(d),2),
         's; p10', round(d[len(d)//10],2), 'p90', round(d[len(d)*9//10],2))
   "
   # 4876 gaps; median 6.71 s; p10 2.66 p90 9.37
   ```
10. Note the wall-clock time of the first and last row of the bracket, from the
    clock you measured in step 6.

### Phase 2 — the swap

11. **Stop the recorder.** Only one process may own `/dev/rfcomm0`, and stopping
    it ends a live drive session — so the vehicle must be stationary.
    ```bash
    ssh <node> 'sudo -n /usr/bin/systemctl stop hummer-drive'
    ```
12. Unplug the OBDLink MX+.
13. Connect the MDI 2 DLC cable to the J1962 and the MDI 2 to the Windows
    machine. The user guide offers USB, Ethernet or WLAN; **prefer a wired
    link**, on the plain grounds that a dropped link mid-capture wastes the
    session and there is no reason to accept that risk in a driveway.

### Phase 3 — GDS2

14. Launch Techline Connect, then **GDS2**. Select the vehicle. **Do not open
    SPS.**
15. **Record the module list GDS2 shows, verbatim**, with the software and
    calibration part numbers it reports for each. This is the free win of the
    whole exercise: GM's own topology, against the eight-address service-09
    census in [GM_MODULE_MAP.md](GM_MODULE_MAP.md). In particular it is the only
    known way to find out **what module `CD` is** — a proven service-22 responder
    that has refused all seventeen identifiers ever put to it, including the four
    ISO 14229-1 standard identification ones.
16. Open **only** the Data Display lists on the target list. Nothing else in the
    menu is in scope ([section 8](#8-the-hard-boundary)).
17. **Bookmark** the start and end of each held state in the session log.
    PIP5632G: *"Placing Bookmarks at points of interest in a Session Log may be
    helpful for TAC to assist you with diagnosing the concern."* A later reader
    here is not TAC, but it is exactly the same problem.
18. **Screenshot every data list**, with parameter names and units legible and a
    clock visible in frame. The screenshots are the durable artefact; see
    step 19 for why.
19. **Export the session log.** Per **PIP5632G** (published 12/5/2022):
    *Launch GDS2 → click **Review Stored Data** → click **Edit** → tick the
    session log → click **Export** → save to the Desktop.* The bulletin warns:
    *"Do not use the 'Save As' tab in GDS2 as this will save the file in a format
    that cannot be viewed by TAC"* — which is also a warning that the export is a
    GDS2-native artefact and may not be readable by anything else we own. Hence
    the screenshots.

### Phase 4 — swap back

20. Close GDS2. Disconnect the MDI 2 from the vehicle.
21. Plug the MX+ back in and restart the recorder — then **prove rows resumed**,
    which is not the same as the unit reporting active:
    ```bash
    ssh <node> 'sudo -n /usr/bin/systemctl start hummer-drive'
    ssh <node> 'sleep 25; journalctl -u hummer-drive -n 3 --no-pager'
    ```

### Phase 5 — second OBD bracket, and the test that decides the session

22. Record at least ten more cycles in the **same** held state.
23. **Compare each target raw field across the two brackets.** A field that reads
    identically in both may be paired with the GDS2 value recorded between them.
    **A field that moved is discarded for this session.** This is the step that
    makes the whole method honest, and it is the one that will be tempting to
    skip when a number nearly matches.
24. Re-read the DTC baseline from step 8. It must be unchanged.

### Phase 6 — afterwards, at a desk

25. For each surviving pair, write down: the GDS2 parameter name **verbatim**,
    its displayed value and unit, the raw hex from bracket one, the raw hex from
    bracket two, and the module and identifier they came from.
26. **Solve for the scaling rather than correlating for it.** Two or more pairs
    at different states determine a slope and an offset directly, which is a
    stronger statement than any correlation coefficient. `field_windows` in
    `analyze.py` enumerates the candidate byte windows a payload could hold —
    single bytes, big-endian `u16`/`s16`, `u24` — which is the search space:
    ```bash
    PYTHONPATH=src python3 -m hummer_obd.decode_fields --dir evidence/sessions --column <col>
    ```
    Note the limitation honestly: `decode_fields._TARGETS` is a fixed tuple of
    columns the vehicle already reports, so a GDS2-supplied reference series is
    **not** picked up automatically. Correlating against one needs either a small
    change to that tuple or the arithmetic done by hand. Say which was done.
27. **Test the candidate scaling against the whole corpus, not the paired
    sample.** A scaling that fits one pair and nothing else is a fit, not a
    decode. This is the step `0x2429` failed: `22534 / 64 = 352.09 V`, which
    across 96 series cells is 3.6676 V — the textbook NMC nominal to four
    figures, from a number nobody fitted — and it was one sample, and it was
    wrong.
28. **Only then edit `confidence.py`**, and cap the result at **level 3**. Level 4
    requires the cross-validation to have been re-derived in more than one
    vehicle state, and one GDS2 session in one held state is one state. Come
    back on a cold morning.

---

## 6. What to point it at first

Ranked. The ordering is by (a) whether a GM document already names a GDS2
display that should contain the quantity, (b) whether our field moves over a
real range in the recorded corpus, and (c) whether it is slow enough to survive
bracketed alternation.

### The named-by-GM shortlist

GM bulletin **24-NA-015** (February 2025) lists `GMC HUMMER EV Pickup 2022–2025`
and `HUMMER EV SUV 2024–2025` in scope, and names these GDS2 displays for
**Ultium Vehicles** verbatim:

```text
K16 Battery Energy Control Module – Data Display – AC Charge History Data
K16 Battery Energy Control Module – Data Display – DC Charge History Data
K16 Battery Energy Control Module – Data Display – Charge Port- Data
K16 Battery Energy Control Module – Data Display – Hybrid/Electric Vehicle Battery AC Charger Data
K16 Battery Energy Control Module – Data Display – Hybrid/Electric Vehicle Battery DC Charger Data
K16 Battery Energy Control Module – Data Display – Thermal Management Propulsion, Battery, and Electronics
K16 Battery Energy Control Module – Data Display – Hybrid Battery Pack Contactor Open Reasons
K16 Battery Energy Control Module – Identification Information – Identification Information
```

The bulletin's own caveat comes first and applies to everything below:
*"Note: Specific GDS verbiage may differ depending on application."* These are
list names, not parameter names. **Nothing here tells us what individual
parameters those lists contain**, and that is the largest single unknown in this
document.

### Tier 1 — go here first

| # | Identifier | Column | Module | What the corpus shows | Where to look in GDS2 |
|---|---|---|---|---|---|
| 1 | `0x40E5` | `coolant_1_raw` | `40` | 905–1250, 63 distinct over 1571 samples | **Thermal Management Propulsion, Battery, and Electronics** — look for a battery/propulsion coolant temperature |
| 2 | `0x40E6` | `coolant_2_raw` | `40` | 413–819, 165 distinct over 1571 samples | Same display; the *second* coolant temperature. Two named temperatures against two moving fields is the cleanest pairing available |
| 3 | `0x2AF1` | `array_2af1` | `CB` | 24 values, stored raw; a Sierra EV log on `wican-fw` #497 shows the same identifier answering 27 bytes | Any battery module temperature list. **This is the biggest prize in the table**: 24 values against the 24-module count three independent structural results agree on |
| 4 | `0x4149` | `evse_current_raw` | `40` | 36–389, only **8 distinct** in 1570 samples — reads 384 parked and unplugged | **Charge Port- Data** and **Hybrid/Electric Vehicle Battery AC Charger Data** — an EVSE advertised/available current. Needs a charge session; a steady AC charge is a held state |
| 5 | `0x5401` | `charger_5401_raw` | `CB` | 0–152, 16 distinct in 4902 samples; plateaus at 147–152 across a measured 1.85–16.51 kW, then decays monotonically to zero over ~3.5 min after a charge ends | **Thermal Management…** — a pump or fan duty. The published label ("charger DC power / 4350") is already known to be wrong here |
| 6 | `0x2709` | `compressor_temp_raw` | `CB` | 107–119, 13 distinct in 1155 samples | **Thermal Management…** — an A/C compressor or refrigerant temperature |

Note the tension in rows 1, 2 and 4: our fields come from **module `40`
(BCM-BodyControl)**, reachable only at CAN priority `0x18`, while the GDS2
displays GM names are on **K16 BECM**. If a GDS2 value on K16 matches a module
`40` byte, that is itself a finding — the body controller is mirroring a battery
signal — and it is worth recording as one rather than glossing.

### Tier 2 — cheap once you are already connected

| Identifier | Column | Module | Corpus | Look for |
|---|---|---|---|---|
| `0x416C` | `group_v1_raw` | `40` | 0–2604, 272 distinct; its most common value is 878, and its three most common values are 878, 2519 and 2513, so it is not clustered around one level | A battery section/group voltage |
| `0x416D` / `0x416E` | `group_v2_raw` / `group_v3_raw` | `40` | Both 0–5023, **6 distinct each**, and they return identical values to each other | Same. Two "independent" group voltages that always agree is a red flag worth resolving |
| `0x434F` | `hv_temp_raw` | `40` | 0–100, 7 distinct | An HV battery temperature |
| `0x4127` / `0x4124` | `batt_temp_a_raw` / `batt_temp_b_raw` | `40` | 234–1048 (5 distinct) / 0–1000 (4 distinct) | Battery temperatures. Both are near-constant, which is itself suspicious |
| `0x2B43` | `array_2b43` | `CB` | 26 values; tracks state of charge at r=+0.995 across the drive corpus | Any 26-element battery list. Twenty-six is not twenty-four, and that gap is unexplained |
| `0x27BF` / `0x27BB` / `0x27B5` | `regen_field_raw` / `thermal_energy_raw` / `thermal_distance_raw` | `CB` | 0–77 / 320–830 / 68–177; 61 / 51 / 51 distinct over 1155 samples | The source's candidate labels are regeneration and thermal-management energy and distance; nothing here confirms any of them. If they turn out to be accumulating counters they will survive a swap trivially — that has not been checked and step 23 checks it |
| `0x2AF5` bytes 6–9 | `cell_extra_raw` | `CB` | 4 trailing bytes; byte 9 constant at 23, byte 7 takes 7 values between 13 and 24 over 1315 replies | If GDS2 names **which** cell or group is weakest/strongest alongside min and max, the small bounded integers are indices and this vehicle can name the cell |
| `0x2429` | `field_2429_raw` | `17` | Moves with load (r=+0.83 vs pack current, r=-0.67 vs pack voltage, across 405 samples on the node); the source calls it *nominal* pack voltage, `/64` | **Hardest of all: it moves in seconds**, so it cannot be paired under alternation. Included only so that if GDS2 shows a load-tracking pack parameter, its name is captured |

**A discrepancy worth knowing before you plan around step 27.** Every count in
the two tables above is measured from the **34 session CSVs committed to this
repository** (4907 rows), which is what a fresh checkout can reproduce. The node
holds more. For most fields that only means the committed figures are a floor —
but for `0x2429` the gap is decisive:

```bash
PYTHONPATH=src python3 -c "
import csv,glob
v=[r['field_2429_raw'] for f in sorted(glob.glob('evidence/sessions/drive-*.csv'))
   for r in csv.DictReader(open(f)) if r.get('field_2429_raw')]
print('field_2429_raw n=',len(v),'distinct=',len(set(v)), sorted(set(v)))
"
# field_2429_raw n= 18 distinct= 1 ['5806']
```

Eighteen rows holding one value, against the 405 samples `confidence.py` cites
for its correlations. **The corpus-wide test in step 27 has almost nothing to
test `0x2429` against in a checkout.** Sync the node's sessions before the
session (step 4) or the last step of the protocol quietly does nothing for that
field.

### Tier 3 — the existence questions

These cost one look at a menu each and cannot be answered any other way.

* **Does GDS2 show motor RPM, torque, inverter or stator temperature, or a
  propulsion/regen power limit?** Five `unsourced` rows in
  [ACCESS_MATRIX.md §3](ACCESS_MATRIX.md#3-what-cannot-be-reached) turn on this.
* **Does GDS2 show individual cell voltages on Ultium?** The largest open gap in
  this project. See [section 9](#9-what-we-do-not-know) — the evidence points
  toward *no*, and it is not settled.
* **What is module `CD`?** GM's module list would name it.
* **Does module `45` (Gateway) have any data displays at all?**
* **Is our `CB` GM's `K16`?** Our vehicle names it `BSM-BatterySysMngr`; GM's
  bulletins say `K16 Battery Energy Control Module`. Nothing has confirmed those
  are the same node.

---

## 7. What to record

The test of this section is whether a reader in a year, who was not there, can
decide whether to believe a decode. Record all of it, in one directory, with the
session date in the name.

**From the GDS2 side**

- The exported session log file (PIP5632G, step 19).
- A screenshot of **every** data list opened, parameter names and units legible.
- The module list GDS2 reported, transcribed, with software and calibration part
  numbers.
- GDS2 software version, its database/build version, MDI 2 firmware version,
  subscription purchase date.
- **What was looked at and found empty**, and what was not looked at at all.
  This is the same discipline as [SOURCING_2026-09-04.md](SOURCING_2026-09-04.md):
  a recorded negative stops the next session repeating the search.

**From the OBD side**

- Both bracket session CSV filenames, and the row index ranges used.
- The raw JSONL transcript for each bracket — the transmission record, byte for
  byte, before anything parsed it:
  ```bash
  PYTHONPATH=src python3 scripts/review_raw_log.py logs/raw/<transcript>.jsonl
  ```
- The DTC baseline before and after, in full, including which module it was
  addressed to.

**The state, from the data rather than from memory**

- `soc_pct`, `energy_kwh`, `pack_v`, `pack_a`, `temp_f`, `volts` at both
  brackets — read out of the recorded row, not recalled.
- Ambient temperature, plugged/unplugged, charge rate if charging, ignition
  state, whether the truck had just been driven.
- The measured clock offset between the two machines.

**The reasoning**

- For each pair: parameter name verbatim, displayed value and unit, both raw
  hex readings, the byte window, the candidate scaling, and what it predicts
  across the rest of the corpus.
- For each pair **discarded** because the field moved between brackets: say so.
  A discarded pair is evidence about the method and costs nothing to write down.

**On the VIN.** A GDS2 session is a session on one specific VIN, the tool
records vehicle identification, and Identification Information is in scope of
this workflow by design. [SAFETY.md](SAFETY.md) forbids an unmasked VIN in the
public repository, and raw transcripts and evidence JSON are already
git-ignored. **Treat the GDS2 export
and every screenshot as VIN-bearing** and keep them with the raw transcripts,
not in a committed artefact. Publish the parameter names, values, units, raw
hex and reasoning; not the file.

---

## 8. The hard boundary

This section is not advisory and it is not a risk assessment. It is the list of
things that make this workflow acceptable, and each of them holds alone.

**1. No SPS, no SPS2, no programming, ever.** Not module programming, not
calibration, not configuration, not setup, not "Hybrid/EV Battery Data
Retrieval" from PIP6009B however much this project wants what is behind it. If
a diagnostics-only subscription is available, buy that one — a subscription that
structurally cannot program is a better guarantee than an intention not to.

**2. GDS2 Data Display and Identification Information only.** GDS2 can also
operate control functions, run configuration and reset functions, and clear
DTCs. None of those is in scope. **Clearing a DTC destroys evidence** and is
forbidden here by the same rule that puts service `04` in `FORBIDDEN_SERVICES`
at every one of the node's five gates.

**3. No bidirectional test without a separate, written decision.** Not a
sub-case of rule 2 but a distinct one, because bidirectional tests are how a
diagnostic tool *proves* a component and are therefore genuinely useful. They
are also how it commands the vehicle. If one is ever wanted, it gets its own
document, its own justification, and its own record — the way service `22` got
one in [SAFETY.md](SAFETY.md) rather than being waved through.

**4. The MDI 2 never touches the Pi.** Not its USB, not its network, not a
script, not a cron job, not "just to see if it enumerates". It connects to a
Windows machine with a human sitting at it, and to nothing else. The MDI 2 is
never left plugged into the vehicle unattended.

**5. Nothing here changes what the node transmits.** No identifier is added to
`ENHANCED_READ_DIDS` as a result of a GDS2 session. A GDS2 parameter name is not
a source that names an identifier, and [SAFETY.md](SAFETY.md)'s five
change-control requirements are unchanged. The output of this workflow is edits
to `confidence.py` and to documentation — nothing else.

**6. Say plainly what this does widen.** During that hour, GM's tool will
transmit far more to the truck than this project ever has: it enumerates every
module, opens diagnostic sessions, and requests whatever a data list contains.
That is a real widening and it is accepted knowingly, bounded to Data Display,
with a human present and the vehicle stationary. **For that hour the operator is
the safety mechanism, not the gate** — which is precisely why it happens on
another machine, in a session with a beginning and an end, and never inside a
process that runs for hours with nobody watching.

**7. The vehicle is stationary throughout.** Every swap ends a recording
session, and a drive is worth more than an hour of anyone's time.

**8. The abort conditions from [ACCESS_MATRIX.md §6](ACCESS_MATRIX.md#6-verifying-against-the-actual-vehicle)
apply unchanged** — a transmit error counter off zero, a bus fault, a new
message on the driver information centre, any change in vehicle behaviour. Pull
the interface first and ask questions afterwards.

---

## 9. What we do not know

Stated as unknowns rather than smoothed over, because the temptation to write
this page as though the session had already happened is real.

**About GDS2's coverage of this vehicle**

* **Whether a non-dealer ACDelco TDS subscription gives the same GDS2 database
  and the same coverage as a dealer terminal.** Not established. GM's bulletins
  are written for dealers.
* **What parameters the K16 lists actually contain.** 24-NA-015 names eight
  entries — seven Data Display lists and one Identification Information list.
  It names not one parameter inside any of them, and its own note says the
  verbiage varies by application. **Every target in
  [section 6](#6-what-to-point-it-at-first) is a guess about list contents.**
* **Whether GDS2 shows per-cell voltages on Ultium.** The nearest evidence is
  negative and is from a different platform: GM TechLink (Mid-May 2021) describes
  the Bolt EV cell inspection as reading the *"average battery cell voltage and
  the minimum cell voltage parameters"* from the HPCM2 Voltage Data screen —
  average and minimum, not 96 individual values, on a vehicle whose signalset
  publicly carries 96 per-cell identifiers. That is suggestive and it is not
  evidence about Ultium. **Unknown.**
* **Whether GDS2 shows motor RPM, torque, inverter or stator temperature, or a
  power limit** on modules `17`, `1D`, `1E`. Nothing found either way.

**About the mapping between GDS2 and what we read**

* **Whether a GDS2 parameter corresponds 1:1 to a service-22 identifier.** It
  may be computed by the tool from several, or scaled differently, or read from a
  module other than the one we ask. If it is composed, a scaling derived from it
  is wrong in a way that will look right. **This is the failure mode most likely
  to produce a confident bad decode from this workflow**, and the corpus-wide
  test in step 27 is the defence against it.
* **Whether `CB` is `K16`, and what `CD` is.** Our vehicle names both
  `BSM-BatterySysMngr`; GM names one `K16 Battery Energy Control Module`.
  Nothing connects those strings.
* **Whether GDS2's Data Display on a 2021+ Global B vehicle requires an
  authenticated session at all.** Third-party sources describe Techline Connect
  handling Secure Gateway authentication and gating *module-level programming*.
  Whether plain data display is gated is not established. If it is, the whole
  workflow depends on a live subscription and a working internet connection at
  the vehicle — plan for that.

**About the connector**

* **Whether the MDI 2 uses CAN FD on this vehicle's diagnostic pair.** Load
  bearing: it is what makes options C and D unacceptable rather than merely
  awkward, and this project has no instrument that could answer it. The MX+
  reaching this truck in classical CAN says what happens for *our* requests and
  nothing about GDS2's.

**About the price**

* **Everything.** `acdelcotds.com` returned HTTP 403 to every attempt.
  Third-party figures conflict by more than an order of magnitude. **Check on the
  day, from GM.**

---

## Sources

GM primary documents, retrieved while drafting this page and text-extracted
locally with `pdftotext`:

- [Bulletin 24-NA-015, February 2025](https://static.nhtsa.gov/odi/tsbs/2025/MC-11015213-0001.pdf) — GDS2 charging data displays for Ultium vehicles; HUMMER EV Pickup 2022–2025 and SUV 2024–2025 in scope
- [PIP6009B, published 12/16/2024](https://static.nhtsa.gov/odi/tsbs/2024/MC-11012017-0001.pdf) — `SPS2 Hybrid/EV Battery Data Retrieval`, and the instruction not to program the BECM
- [PIP5632G, published 12/5/2022](https://static.nhtsa.gov/odi/tsbs/2022/MC-10230683-9999.pdf) — GDS2 session log export procedure
- [GM TechLink, Mid-May 2021](https://gm-techlink.com/wp-content/uploads/2021/06/GM_TechLink_10_Mid-May_2021.pdf) — Bolt EV cell inspection via HPCM2 Voltage Data
- [Multiple Diagnostic Interface 2 User Guide](https://freemen.su/inst/OEM-GM-MDI2-ENG.pdf) — GM Customer Care and Aftersales; J1962 ETE cable, USB/Ethernet/WLAN host link, J2534 pass-thru, powered from the vehicle's 12 V. *(GM-authored document, retrieved from a third-party mirror; the copyright notice is GM's.)*

Secondary, and labelled as such wherever cited above:

- [Fleet Maintenance, GDS2 product entry](https://www.fleetmaintenance.com/shop-operations/shop-management/product/12088885/general-motors-llc-global-diagnostic-system-2-gds2) — model year coverage and function list
- [Indie Garage: ACDelco TDS subscription changes take effect July 1](https://www.indiegarage.ca/acdelco-tds-subscription-changes-take-effect-july-1/) — SPS per-VIN 24 months → 30 days
- [TechRoute66: ACDelco TDS explained](https://techroute66.com/acdelco-tds) and [Techline Connect subscription listing](https://techroute66.com/product/gm-usa-online-subscription-techline-connect-sps2-and-gds2) — reseller, pricing not GM's
- [AE Solutions: GM GDS2](https://aesolutions.us/products/gm-gds2-light-heavy-duty) — "GDS2 subscriptions are strictly for diagnostics"
- [Diagnostic Network: Gen 1 Volt battery data log with GDS2](https://diag.net/msg/m1oreow27vvqhsbte48agnqnm7) — bookmarks, plots and data export in practice
