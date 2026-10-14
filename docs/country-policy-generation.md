# Batch country-policy generation

Generate proposed municipal levels from the public [OSM administrative-level table](https://wiki.openstreetmap.org/wiki/Template:Admin_level), using [Unicode CLDR territory names](https://github.com/unicode-org/cldr-json) to identify country codes. This is an offline development tool, not a runtime provider or a geography import. One pass processes the whole table; city data is still indexed only when a retained activity needs it.

Download the two small public inputs, then run the standard-library-only script from the repository root:

```sh
rtk proxy curl -fL --max-time 30 https://wiki.openstreetmap.org/wiki/Template:Admin_level -o /tmp/admin-levels.html
rtk proxy curl -fL --max-time 30 https://raw.githubusercontent.com/unicode-org/cldr-json/91c267402229a59e3ef2774544f001bf959e8809/cldr-json/cldr-localenames-full/main/en/territories.json -o /tmp/country-names.json
rtk proxy python3 scripts/dev/generate-country-policies.py --source /tmp/admin-levels.html --countries /tmp/country-names.json --output /tmp/country-policy-candidates.json --policies-output /tmp/country-policy-proposals.json
```

The report retains existing curated policies and selector profiles, adds clear candidates, and records ambiguous entries with their table evidence. It records source checksums so a result can be checked against the same saved inputs. Repeating the command with `--check` compares the result without writing it.

The [captured report](data/country-policy-candidates.json) processes all 199 country/territory groups in OSM table revision 3104571: 37 curated policies, no outstanding clear candidates, 161 entries requiring review, and one unresolved code (Kosovo). The initial pass proposed AZ 8, BJ 6 and IS 6; their activation uses the rules in [the provisioning contract](city-provisioning.md#qualified-municipal-rules). Azerbaijan requires a positive municipality guard because its actual level-8 catalog also includes city districts. The [proposed level map](data/country-policy-proposals.json) now contains the current policies. It has the same shape as `CITY_ADMIN_LEVELS`; it is not enabled by generating it. `--check` requires the same saved input bytes, not a later live wiki response. The source revision and checksums are recorded in the report.

Generation never changes `DEFAULT_ADMIN_LEVELS` or enables additional countries. Proposed levels must be reviewed before activation; a level alone cannot express province-specific tiers, municipality proof, authority-code guards or exact municipal exceptions. The existing Azerbaijan, UK, Canada and US profiles remain authoritative. Missing, proposed, mixed or conditional evidence is kept for review rather than assigned a generic level-eight fallback.

The generated report is a batch worklist, not evidence of worldwide coverage. Activating a new policy still requires municipal classification and actual bounded provider/import checks from [the provisioning contract](city-provisioning.md). No private recordings, database access, planet downloads or cloud resources are involved.
