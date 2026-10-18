"""Where the data lives in the triple store.

Each patient file is loaded into its own named graph `http://example.org/graph/<file name>`
(see hackathon-virtuoso-release/README.md), so a patient graph IRI identifies one patient.
The SPHN terminologies are loaded into the graph `https://biomedit.ch/rdf/sphn-resource/global`.
"""

PATIENT_GRAPH_PREFIX = "http://example.org/graph/"
TERMINOLOGY_GRAPH = "https://biomedit.ch/rdf/sphn-resource/global"
