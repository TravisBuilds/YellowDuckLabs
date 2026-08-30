"""West Vancouver-specific source code.

This package, together with ``firewatch/municipalities/west-vancouver.yaml``, is
the only place West-Vancouver-specific logic is permitted. Nothing in
``firewatch/core`` may import it.

It is currently empty: the District's holdings are reachable through the generic
``arcgis_feature_service`` adapter, so no bespoke code is required. That is the
intended outcome. A subclass belongs here only when the District publishes
something the generic adapters genuinely cannot express.
"""
