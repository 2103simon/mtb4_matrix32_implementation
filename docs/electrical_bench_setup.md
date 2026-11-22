# CH1 Matrix32 dummy-load and oscilloscope wiring

This is the **current documentation reference for CH1-only integrated recordings** in this repository. It adapts the operator-reported bench wiring from [the source project's bench-load topology](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/docs/bench_load_topology.md), rather than independently claiming an electrical inspection or safety certification.

> **Only Matrix32 CH1 is stimulated and measured as a Matrix32 output here.** Do not connect a person or stimulation electrodes. Confirm actual wiring and earth-reference compatibility with the responsible operator before an armed experiment. Prior successful recordings do **not** establish safety.

## Three-branch physical dummy load (CH1-only stimulation)

| Property | Documented setup | Status |
| --- | --- | --- |
| Stimulator | E-Matrix32 channel **CH1 cathode** and its **shared anode** | Operator-reported source topology |
| Dummy-load resistor | **R1 = 100.0 kΩ** between CH1 cathode and shared anode | Operator-reported meter measurement; tolerance/rating not provided |
| Oscilloscope | Tektronix **MDO4104B-6** | Source-project instrument |
| CH1 probe | Tektronix **TPP1000 10× passive probe**; tip/hook on CH1 cathode; ground reference on Matrix32 shared anode | Operator-reported assembly |
| Gray clock | Separate **Nano Every D7 → scope CH2** | This project's acquisition configuration |
| Matrix32 CH2 cathode | **R2 = 99.4 kΩ** to shared anode; **physically connected** | Operator confirmed same breadboard arrangement as source rig |
| Matrix32 CH3 cathode | **R3 = 99.5 kΩ** to shared anode; **physically connected** | Operator confirmed same breadboard arrangement as source rig |
| Matrix32 CH4–CH32 | **Open/unconnected** in the referenced bench topology | Operator confirmed exact same setup as source rig |
| Spare resistor | **992 kΩ** positioned beside CH2 resistor but **not connected** | Unchanged from source-rig description |

### Functional connection diagram

```text
Matrix32 CH1 cathode ----+---- scope CH1 TPP1000 probe tip
                         |
                         +----[ R1 = 100.0 kOhm ]----+
Matrix32 CH2 cathode ---------[ R2 =  99.4 kOhm ]----+---- Matrix32 common anode
Matrix32 CH3 cathode ---------[ R3 =  99.5 kOhm ]----+           |
                                                                 +-- probe ground reference*

Matrix32 CH4-CH32: OPEN / no connected resistors
992 kOhm spare near CH2: NOT ELECTRICALLY CONNECTED

Nano Every D7 -------------------------------------> scope CH2 tip
Nano ground ---------------------------------------> scope CH2 reference†
```

`*` In the referenced setup, passive probe ground clips were connected to the **common anode**; ordinary oscilloscope BNC shells / probe grounds are protective-earth referenced. **That makes this a potentially earth-referenced measurement, not an isolated differential measurement.** Do not mistake shared wiring for isolation.

`†` CH2 is a **separate Nano reference circuit**. A standard multichannel scope typically has a common reference across channels. The actual Nano ground, Matrix32 common anode and protective-earth topology **must be reviewed together** before enabling Matrix32 output; a diagram showing separate signal paths does not demonstrate electrical isolation.

**Operator confirmation:** This project uses **exactly the same physical three-branch resistor breadboard** as the source repository: R1, R2, and R3 all connect their respective cathodes to the **same shared anode**, in parallel as separate cathode-to-anode branches, **not as three resistors connected in series**. Only the Matrix32 **CH1 output is commanded** and its physical output is recorded here on oscilloscope CH1; the connected CH2/CH3 branches remain part of the physical circuit and may affect interpretation of the shared node or inactive-channel pickup. The 992 kΩ spare remains disconnected, as documented for the identical reference setup. Scope CH2 in *this project* carries the Nano D7 Gray marker; it does **not** measure Matrix32 cathode CH2.

## Scope settings in the integrated recording

| Scope setting | Default |
| --- | --- |
| CH1 | Matrix32 CH1 dummy-load voltage, **10 V/div requested**, operator must check safe actual full-scale and attenuation |
| CH2 | Nano Every D7 (nominal 0–5 V), **2 V/div** |
| Trigger | CH2 rising, **+1 V**, horizontal position **0%**, delay OFF |
| Sample mode | SAMPLE, SEQUENCE/stop-after acquisition |
| Horizontal scale | **2 s/div**, nominal 20-second finite record |
| Record length | **1,000,000 points**, nominal **20 µs/sample** |
| Interface | USBTMC to host; downloaded `waveforms/record_00001_ch1.csv` and `record_00001_ch2.csv` |

**Probe attenuation, coupling, terminations, voltage headroom and calibration must be read back/checked on the actual instrument.** The fact that a TPP1000 is nominally 10× does not certify software or scope channel settings.

## What CH1 voltage does and does not measure

For an independently verified differential voltage across **R1 = 100.0 kΩ**, ideal resistor current is `I_R1(t) = V_R1(t) / 100000 Ω`. This measures only the **resistor branch current** under the assumed circuit model. It is not directly the total device output current, delivered charge, or proof that a Matrix32 `SC` or `SV` command achieved its named value. Passive probe resistance/capacitance and shared-node movements may alter the waveform. The source reference identifies TPP1000 with pogo-tip and hook/grabber; the exact assembled probe parasitics are not separately measured.

For example, **if** 50 V was independently measured *across* the resistor, the idealized branch current would be 0.50 mA. **Sending `SV=50` does not constitute that voltage measurement.** Do not use this example as a safety limit.

## Electrical safety gate

Before live `--run --arm --dummy-load-confirmed` use:

1. Confirm **CH1 cathode → 100.0 kΩ rated resistor → shared anode** using a suitable powered-down test procedure. Document resistor voltage/power ratings and independent verification if available.
2. Confirm the **three physically connected branches** remain as documented: CH1–R1=100.0 kΩ, CH2–R2=99.4 kΩ, CH3–R3=99.5 kΩ, all to the shared anode; the 992 kΩ spare remains disconnected and CH4–CH32 remain open. CH1-only *software commands* do not physically remove the CH2/CH3 resistor paths.
3. Confirm scope probe ground/BNC earth continuity and **whether the Matrix32 manufacturer/provider approves earth-referencing its shared anode** in this exact configuration. The referenced operator reported provider-recommended similar wiring but no independently archived manufacturer isolation approval.
4. Confirm CH2 Nano reference wiring is compatible with the CH1 scope reference arrangement; do not tie outputs/grounds together by assumption or defeat the oscilloscope's protective earth.
5. Confirm correct probe attenuation, headroom, chosen CH1 V/div, isolated/differential probing **where needed**, an accessible physical disconnect, and no person/electrodes attached.
6. Begin with a non-stimulating `--run` baseline. A software STOP or BLE ACK is **not** evidence of an electrically safe/off output; verify the result physically.

If any grounding/isolation condition is uncertain, **do not perform an armed trial** until it is reviewed. This document records the reported rig; it does not certify it.

## References

- [Source project: full multi-channel dummy-load wiring, photos and limitations](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/docs/bench_load_topology.md)
- [Source project: Tektronix USB bench setup](https://github.com/2103simon/matrix32_nerve_stimulation/blob/cleanup/analysis-consolidation/docs/tektronix_mdo4104b_usb.md)
- [Integrated recording procedure](integrated_matrix32_scope_recording.md)
- [Installation and safety checks](installation_and_operations.md)
- [Tektronix MDO4000B user manual (scope grounding)](https://download.tek.com/manual/MDO4000B-Oscilloscope-User-Manual_0.pdf)
