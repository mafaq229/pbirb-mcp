# Bundled schema attribution

`reportdefinition.xsd` is the Microsoft RDL 2016/01 XML Schema Definition.

Copyright (c) Microsoft Corporation. All rights reserved. The schema header
preserves the original Microsoft copyright + warranty disclaimer verbatim.

## Redistribution permission

The MS-RDL Open Specifications documentation (which carries this schema in
section 5.8) grants redistribution under the standard "Intellectual Property
Rights Notice for Open Specifications Documentation":

> "You can also distribute in your implementation, with or without
> modification, any schemas, IDLs, or code samples that are included in the
> documentation."

— [\[MS-RDL\]: Report Definition Language File Format][ms-rdl-spec], "Intellectual
Property Rights Notice for Open Specifications Documentation" → "Copyrights".

The same clause appears on every Open Specifications document where Microsoft
publishes inline schemas (BIRT and JasperReports operate under the same posture
for their RDL XSD bundles).

## Source

The bundled file is byte-identical to the schema published by Microsoft in
`microsoft/RdlMigration` ([RdlMigration.UnitTest/reportdefinition.xsd][rdl-migration-xsd]).
That repository's copy matches the inline schema on [\[MS-RDL\]: RDL XML Schema
for Version 2016/01][ms-rdl-2016] (section 5.8 of MS-RDL).

Target namespace: `http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition`.

## Patents (NOT granted by this notice)

MS-RDL is **not** listed in the Microsoft Open Specifications Promise nor the
Microsoft Community Promise covered-specifications tables. Implementations that
require a written patent license should contact `iplg@microsoft.com`. For a
parser/editor/validator that does not implement RDL rendering algorithms, the
patent surface stays documentation-grade.

## Updating

To refresh the bundled XSD when Microsoft publishes a newer RDL 2016/01
revision:

```bash
curl -sLO https://raw.githubusercontent.com/microsoft/RdlMigration/master/RdlMigration.UnitTest/reportdefinition.xsd
diff -u pbirb_mcp/schemas/reportdefinition.xsd reportdefinition.xsd
mv reportdefinition.xsd pbirb_mcp/schemas/reportdefinition.xsd
.venv/bin/python -m pytest tests/test_schema_bundled.py -v
```

Then run the full test suite to confirm nothing regresses against the new
schema, and update this NOTICE if the source URL changes.

[ms-rdl-spec]: https://learn.microsoft.com/en-us/openspecs/sql_server_protocols/ms-rdl/53287204-7cd0-4bc9-a5cd-d42a5925dca1
[ms-rdl-2016]: https://learn.microsoft.com/en-us/openspecs/sql_server_protocols/ms-rdl/52ce3983-2bfc-4e72-9359-42aaf5fe4509
[rdl-migration-xsd]: https://github.com/microsoft/RdlMigration/blob/master/RdlMigration.UnitTest/reportdefinition.xsd
