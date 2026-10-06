# Rev C manufacturing files

| File | Use |
| --- | --- |
| [Gerbers](dialback-zero-rev-c-jlcpcb-gerbers.zip) | PCB layers and drill files |
| [Factory BOM](dialback-zero-rev-c-jlcpcb-bom.csv) | 41 rows covering 89 factory-installed components |
| [CPL](dialback-zero-rev-c-jlcpcb-cpl.csv) | Matching top-side component positions and rotations |
| [Complete BOM](design-bom.csv) | All 104 component references |
| [Manifest](manifest.json) | File checksums and design identity |

Upload the Gerber ZIP for PCB fabrication. For JLCPCB assembly, also upload the
factory BOM and CPL together. The CPL already includes the supplier-specific
rotations and USB-C connector offset; check the placement preview before ordering.

The 15 remaining components are soldered by hand after factory assembly.
See the [parts list](../../docs/self-solder-parts.md) and
[assembly guide](../../docs/assembly.md). J1 is the only underside component.
