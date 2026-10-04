"""Offline parsing tests for the parliament, dictionary, traffic-event, grid and ADEM tools."""

import json
import unittest
from xml.etree import ElementTree

from luxembourg_mcp.http import UpstreamError
from luxembourg_mcp.providers import DATA_PUBLIC_RESOURCE_HOSTS, LuxembourgData, _entsoe_points


class ScriptedHttp:
    """Serves queued responses and records each call's URL, headers and host allowlist."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, url, headers=None, allowed_hosts=None):
        self.calls.append({"url": url, "headers": headers, "allowed_hosts": allowed_hosts})
        if not self.responses:
            raise AssertionError(f"no scripted response left for {url}")
        return self.responses.pop(0)

    def get_json(self, url, headers=None, allowed_hosts=None):
        return self._next(url, headers, allowed_hosts)

    def get_json_value(self, url, headers=None, allowed_hosts=None):
        return self._next(url, headers, allowed_hosts)

    def get_bytes(self, url, headers=None, allowed_hosts=None):
        return self._next(url, headers, allowed_hosts), "utf-8"


def dataset(slug, resources):
    return {
        "slug": slug,
        "page": f"https://data.public.lu/en/datasets/{slug}/",
        "resources": [{"format": fmt, "url": url, "title": title, "last_modified": modified}
                      for fmt, url, title, modified in resources],
    }


class AllowlistAssertions:
    def assertDownloadsAllowlisted(self, http):
        downloads = [call for call in http.calls if call["url"].startswith("https://download.data.public.lu/")]
        self.assertTrue(downloads)
        for call in downloads:
            self.assertEqual(call["allowed_hosts"], DATA_PUBLIC_RESOURCE_HOSTS, call["url"])


# The Chamber exports cp1252, which is what makes "Voté" and "Pas participé" interesting.
VOTES_CSV = (
    "meeting_date,vote_name,vote_type,person_title,lastname,firstname,rattachement_abrv,rattachement_type,vote_result\n"
    '"2026-07-15 19:54:46","MO1 - PL8727","Voté","Madame","Tanson","Sam","déi gréng","Sensibilité politique","Non"\n'
    '"2026-07-15 19:54:46","MO1 - PL8727","Voté","Monsieur","Clement","Sven","Piraten","Sensibilité politique","Oui"\n'
    '"2026-07-15 19:54:46","MO1 - PL8727","Voté","Monsieur","Keup","Fred","ADR","Groupe politique","Pas participé"\n'
    '"2026-07-15 18:31:53","PL 8688 - Extension ligne tramway","Premier vote constitutionnel","Madame","Tanson","Sam","déi gréng","Sensibilité politique","Oui"\n'
    '"2026-07-15 18:31:53","PL 8688 - Extension ligne tramway","Premier vote constitutionnel","Monsieur","Baum","Marc","déi Lénk","Sensibilité politique","Abstention"\n'
    '"2026-07-15 18:31:53","PL 8688 - Extension ligne tramway","Premier vote constitutionnel","Madame","Beissel","Simone","DP","Groupe politique","Oui"\n'
    '"2025-10-30 14:37:37","PL 8448 - Centre de remisage du tramway","Voté","Monsieur","Baum","Marc","déi Lénk","Sensibilité politique","Non"\n'
).encode("cp1252")


class PlenaryVoteTests(AllowlistAssertions, unittest.TestCase):
    @staticmethod
    def metadata():
        return dataset("votes", [
            ("xml", "https://download.data.public.lu/old/109-votes.xml", "votes", "2024-07-15T14:31:14+00:00"),
            ("csv", "https://download.data.public.lu/old/109-votes.csv", "votes", "2024-07-15T14:31:00+00:00"),
            ("csv", "https://download.data.public.lu/new/109-votes.csv", "votes", "2026-10-04T03:30:20+00:00"),
        ])

    def votes(self, **arguments):
        http = ScriptedHttp([self.metadata(), VOTES_CSV])
        return http, LuxembourgData(http).get_plenary_votes(**arguments)

    def test_groups_ballots_into_votes_newest_first(self):
        http, result = self.votes()
        self.assertEqual(http.calls[1]["url"], "https://download.data.public.lu/new/109-votes.csv")
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual([vote["date"] for vote in result["votes"]],
                         ["2026-07-15 19:54:46", "2026-07-15 18:31:53", "2025-10-30 14:37:37"])
        motion = result["votes"][0]
        self.assertEqual((motion["subject"], motion["type"]), ("MO1 - PL8727", "Voté"))
        self.assertEqual(motion["tally"], {"yes": 1, "no": 1, "abstain": 0, "did_not_vote": 1})
        self.assertEqual(motion["by_party"]["déi gréng"], {"no": 1})
        self.assertEqual(motion["by_party"]["ADR"], {"did_not_vote": 1})
        self.assertNotIn("deputy_votes", motion)
        self.assertDownloadsAllowlisted(http)

    def test_query_matches_subject_accent_insensitively(self):
        _, result = self.votes(query="TRAMWAY")
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["votes"][0]["tally"], {"yes": 2, "no": 0, "abstain": 1, "did_not_vote": 0})
        self.assertEqual(result["votes"][0]["type"], "Premier vote constitutionnel")

    def test_deputy_filter_keeps_only_their_votes_and_reports_choice(self):
        _, result = self.votes(deputy="baum")
        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["votes"][0]["deputy_votes"], [{"deputy": "Marc Baum", "party": "déi Lénk", "vote": "abstain"}])
        self.assertEqual(result["votes"][1]["deputy_votes"][0]["vote"], "no")

    def test_limit_caps_votes_but_not_total(self):
        _, result = self.votes(limit=1)
        self.assertEqual((result["count"], result["total_matches"]), (1, 3))

    def test_unknown_deputy_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "Nobody"):
            self.votes(deputy="Nobody")

    def test_unexpected_layout_is_an_upstream_error(self):
        http = ScriptedHttp([self.metadata(), b"a,b\n1,2\n"])
        with self.assertRaises(UpstreamError):
            LuxembourgData(http).get_plenary_votes()


DEPUTIES_CSV = (
    "per_titre,pph_nom,pph_prenom,date_debut_depute,rattachement_abrv,rattachement_type,pph_date_naissance,address,phone_ext,phone_mobile,email,derniere_circonscription\n"
    '"Madame","Agostino","Barbara","2023-11-21 00:00:00","DP","Groupe politique","1982-07-06","p.a. Groupe politique DP, 9, rue du Saint-Esprit","","","bagostino@chd.lu","Circonscription Sud"\n'
    '"Madame","Adehm","Diane","2023-10-24 00:00:00","CSV","Groupe politique","1970-09-13","21, rue Ferdinand Kuhn, L-1867, Howald","+352298201","621 35 62 72","dadehm@chd.lu","Circonscription Centre"\n'
    '"Monsieur","Baum","Marc","2023-10-24 00:00:00","déi Lénk","Sensibilité politique","1970-01-01","1, rue Privée, Esch","","691 00 00 00","\\\\ ","Circonscription Sud"\n'
).encode("cp1252")


class DeputyTests(AllowlistAssertions, unittest.TestCase):
    def deputies(self, **arguments):
        metadata = dataset("deputies", [("csv", "https://download.data.public.lu/105-depute.csv", "deputies", "2026-10-04")])
        http = ScriptedHttp([metadata, DEPUTIES_CSV])
        return http, LuxembourgData(http).get_deputies(**arguments)

    def test_never_exposes_private_contact_details(self):
        http, result = self.deputies()
        dumped = json.dumps(result, ensure_ascii=False)
        for private in ("Ferdinand Kuhn", "rue Privée", "621 35 62 72", "691 00 00 00", "1970-09-13", "1982-07-06"):
            self.assertNotIn(private, dumped)
        self.assertEqual(set(result["deputies"][0]), {"name", "title", "party", "affiliation", "constituency", "since", "email"})
        self.assertDownloadsAllowlisted(http)

    def test_shapes_and_sorts_by_last_name(self):
        _, result = self.deputies()
        self.assertEqual([item["name"] for item in result["deputies"]], ["Diane Adehm", "Barbara Agostino", "Marc Baum"])
        adehm = result["deputies"][0]
        self.assertEqual((adehm["constituency"], adehm["since"], adehm["email"]), ("Centre", "2023-10-24", "dadehm@chd.lu"))
        self.assertIsNone(result["deputies"][2]["email"])
        self.assertEqual(result["seats_by_party"], {"DP": 1, "CSV": 1, "déi Lénk": 1})

    def test_query_filters_on_name_party_or_constituency(self):
        self.assertEqual(self.deputies(query="sud")[1]["count"], 2)
        self.assertEqual(self.deputies(query="dei lenk")[1]["deputies"][0]["name"], "Marc Baum")
        self.assertEqual(self.deputies(query="Adehm")[1]["count"], 1)

    def test_seat_totals_ignore_the_filter(self):
        _, result = self.deputies(query="Centre")
        self.assertEqual(sum(result["seats_by_party"].values()), 3)

    def test_unknown_deputy_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.deputies(query="Atlantis")


LOD_SEARCH = {"description": "", "results": [
    {"id": "HAUS1", "article_id": "HAUS1", "word_lb": "Haus", "pos": "SUBST+N", "meanings": []},
    {"id": "../ADMIN", "article_id": "../ADMIN", "word_lb": "Bogus", "pos": "SUBST+N"},
    {"id": "PENSIOUN1", "article_id": "PENSIOUN1", "word_lb": "Pensioun", "pos": "SUBST+F"},
]}
LOD_ENTRY = {"entry": {
    "lod_id": "HAUS1", "lemma": "Haus", "partOfSpeechLabel": "SUBST+N", "ipa": "hæːʊs",
    "microStructures": [{"grammaticalUnits": [{"meanings": [
        {"number": 1, "inflection": {"forms": [{"content": "Haiser"}, {"content": "Haus"}]},
         "targetLanguages": {
             "fr": {"parts": [{"type": "translation", "content": "maison"}, {"type": "semanticClarifier", "content": "habitation"}]},
             "en": {"parts": [{"type": "translation", "content": "house"}]},
             "de": {"parts": []}},
         "examples": [{"parts": [{"type": "text", "parts": [{"type": "word", "content": "eist"}, {"type": "word", "content": "neit"},
                                                             {"type": "inflectedHeadword", "content": "Haus"}]}]},
                      {"parts": [{"type": "text", "parts": [{"type": "inflectedHeadword", "content": "Haiser"}]}]},
                      {"parts": [{"type": "text", "parts": [{"type": "word", "content": "third"}]}]}]},
        {"number": 2, "inflection": {"forms": [{"content": "Haiser"}]},
         "targetLanguages": {"en": {"parts": [{"type": "translation", "content": "house"}, {"type": "semanticClarifier", "content": "dynasty"}]}}},
    ]}]}],
}}


class DictionaryTests(unittest.TestCase):
    def test_searches_then_shapes_each_entry(self):
        http = ScriptedHttp([LOD_SEARCH, LOD_ENTRY])
        result = LuxembourgData(http).lookup_luxembourgish("Haus", limit=1)
        self.assertEqual(http.calls[0]["url"], "https://lod.lu/api/en/search?query=Haus&lang=lb")
        self.assertEqual(http.calls[1]["url"], "https://lod.lu/api/en/entry/HAUS1")
        self.assertEqual((result["total_hits"], result["count"]), (3, 1))
        entry = result["entries"][0]
        self.assertEqual((entry["lemma"], entry["ipa"], entry["part_of_speech"]), ("Haus", "hæːʊs", "SUBST+N"))
        self.assertEqual(entry["forms"], ["Haiser", "Haus"])
        self.assertEqual(entry["url"], "https://lod.lu/artikel/HAUS1")
        first, second = entry["meanings"]
        self.assertEqual(first["translations"], {"fr": "maison (habitation)", "en": "house"})
        self.assertEqual(first["examples"], ["eist neit Haus", "Haiser"])
        self.assertEqual(second["translations"]["en"], "house (dynasty)")
        self.assertEqual(second["examples"], [])

    def test_upstream_ids_that_are_not_plain_are_never_fetched(self):
        http = ScriptedHttp([LOD_SEARCH, LOD_ENTRY, {"entry": {"lemma": "Pensioun"}}])
        result = LuxembourgData(http).lookup_luxembourgish("Haus", limit=3)
        self.assertEqual([call["url"] for call in http.calls[1:]],
                         ["https://lod.lu/api/en/entry/HAUS1", "https://lod.lu/api/en/entry/PENSIOUN1"])
        self.assertEqual([entry["id"] for entry in result["entries"]], ["HAUS1", "PENSIOUN1"])

    def test_foreign_language_lookup_uses_that_locale(self):
        http = ScriptedHttp([{"results": []}])
        result = LuxembourgData(http).lookup_luxembourgish(" maison ", language="FR")
        self.assertEqual(http.calls[0]["url"], "https://lod.lu/api/fr/search?query=maison&lang=fr")
        self.assertEqual((result["word"], result["count"]), ("maison", 0))

    def test_rejects_bad_input_before_fetching(self):
        data = LuxembourgData(ScriptedHttp([]))
        with self.assertRaises(ValueError):
            data.lookup_luxembourgish("  ")
        with self.assertRaises(ValueError):
            data.lookup_luxembourgish("Haus", language="es")

    def test_default_fetches_two_entries(self):
        hits = {"results": [{"id": f"W{index}"} for index in range(4)]}
        http = ScriptedHttp([hits, {"entry": {}}, {"entry": {}}])
        self.assertEqual(LuxembourgData(http).lookup_luxembourgish("x")["count"], 2)
        self.assertEqual(len(http.calls), 3)

    def test_limit_is_capped_at_five_entries(self):
        hits = {"results": [{"id": f"W{index}"} for index in range(8)]}
        http = ScriptedHttp([hits] + [{"entry": {}}] * 5)
        self.assertEqual(LuxembourgData(http).lookup_luxembourgish("x", limit=50)["count"], 5)


CITA_EVENTS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<d2:payload xsi:type="sit:SituationPublication" lang="fr" modelBaseVersion="3" xmlns:com="http://datex2.eu/schema/3/common"
  xmlns:loc="http://datex2.eu/schema/3/locationReferencing" xmlns:d2="http://datex2.eu/schema/3/d2Payload"
  xmlns:sit="http://datex2.eu/schema/3/situation" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <com:publicationTime>2026-10-04T18:50:02+02:00</com:publicationTime>
  <sit:situation id="237478">
    <sit:situationRecord xsi:type="sit:MaintenanceWorks" id="237478-2" version="6">
      <sit:situationRecordVersionTime>2026-10-04T11:35:52+02:00</sit:situationRecordVersionTime>
      <sit:validity><com:validityTimeSpecification>
        <com:overallStartTime>2026-10-04T04:48:15+02:00</com:overallStartTime>
        <com:overallEndTime>2026-10-04T20:00:00+02:00</com:overallEndTime>
      </com:validityTimeSpecification></sit:validity>
      <sit:impact><sit:numberOfLanesRestricted>1</sit:numberOfLanesRestricted><sit:numberOfOperationalLanes>1</sit:numberOfOperationalLanes></sit:impact>
      <sit:generalPublicComment><sit:comment><com:values><com:value lang="lb">2 Pisten ageengt</com:value></com:values></sit:comment>
        <sit:commentType>roadworksName</sit:commentType></sit:generalPublicComment>
      <sit:locationReference xsi:type="loc:LocationGroupByList">
        <loc:locationContainedInGroup xsi:type="loc:LinearLocation"><loc:supplementaryPositionalDescription>
          <loc:lengthAffected>1200.0</loc:lengthAffected>
          <loc:locationDescription><com:values><com:value lang="fr">Section courante</com:value></com:values></loc:locationDescription>
          <loc:roadInformation><loc:roadDestination>Autoroute d'Esch, vers Hollerich</loc:roadDestination><loc:roadName>A4</loc:roadName></loc:roadInformation>
        </loc:supplementaryPositionalDescription></loc:locationContainedInGroup>
        <loc:locationContainedInGroup xsi:type="loc:PointLocation"><loc:pointByCoordinates><loc:pointCoordinates>
          <loc:latitude>49.587112</loc:latitude><loc:longitude>6.087343</loc:longitude></loc:pointCoordinates></loc:pointByCoordinates></loc:locationContainedInGroup>
      </sit:locationReference>
      <sit:roadMaintenanceType>maintenanceWork</sit:roadMaintenanceType>
    </sit:situationRecord>
  </sit:situation>
  <sit:situation id="233964">
    <sit:situationRecord xsi:type="sit:EquipmentOrSystemFault" id="233964-1" version="3">
      <sit:locationReference xsi:type="loc:LocationGroupByList"><loc:locationContainedInGroup xsi:type="loc:LinearLocation">
        <loc:supplementaryPositionalDescription><loc:roadInformation><loc:roadName>A13</loc:roadName></loc:roadInformation></loc:supplementaryPositionalDescription>
      </loc:locationContainedInGroup></sit:locationReference>
      <sit:equipmentOrSystemFaultType>notWorking</sit:equipmentOrSystemFaultType>
    </sit:situationRecord>
  </sit:situation>
</d2:payload>"""


class TrafficEventTests(unittest.TestCase):
    def test_shapes_every_situation_record(self):
        http = ScriptedHttp([CITA_EVENTS_XML])
        result = LuxembourgData(http).get_traffic_events()
        self.assertEqual(http.calls[0]["url"], "https://www.cita.lu/info_trafic/datex/situationrecord36")
        self.assertEqual((result["published"], result["count"]), ("2026-10-04T18:50:02+02:00", 2))
        works, fault = result["events"]
        self.assertEqual((works["kind"], works["detail"], works["road"]), ("MaintenanceWorks", "maintenanceWork", "A4"))
        self.assertEqual((works["comment"], works["location"]), ("2 Pisten ageengt", "Section courante"))
        self.assertEqual((works["start"], works["end"]), ("2026-10-04T04:48:15+02:00", "2026-10-04T20:00:00+02:00"))
        self.assertEqual((works["lanes_restricted"], works["lanes_open"], works["length_m"]), (1, 1, 1200))
        self.assertEqual((works["latitude"], works["longitude"]), (49.587112, 6.087343))
        self.assertEqual((fault["kind"], fault["detail"], fault["end"], fault["comment"]),
                         ("EquipmentOrSystemFault", "notWorking", None, None))

    def test_road_filter_is_case_insensitive(self):
        result = LuxembourgData(ScriptedHttp([CITA_EVENTS_XML])).get_traffic_events(road="a13")
        self.assertEqual((result["road"], result["count"]), ("A13", 1))
        self.assertEqual(result["events"][0]["id"], "233964-1")

    def test_malformed_road_is_rejected_before_fetching(self):
        for road in ("A4; DROP", "../x", ""):
            with self.subTest(road=road), self.assertRaises(ValueError):
                LuxembourgData(ScriptedHttp([])).get_traffic_events(road=road)

    def test_entity_declarations_are_an_upstream_error(self):
        with self.assertRaises(UpstreamError):
            LuxembourgData(ScriptedHttp([b'<!DOCTYPE r [<!ENTITY a "x">]><r>&a;</r>'])).get_traffic_events()


def gl_document(*series):
    return ('<?xml version="1.0" encoding="utf-8"?><GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">'
            + "".join(series) + "</GL_MarketDocument>").encode("utf-8")


def time_series(points, extra="", resolution="PT15M"):
    rows = "".join(f"<Point><position>{position}</position><quantity>{value}</quantity></Point>" for position, value in points)
    return (f"<TimeSeries>{extra}<Period><timeInterval><start>2026-10-02T22:00Z</start><end>2026-10-03T22:00Z</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{rows}</Period></TimeSeries>")


def flow_document(origin, target, points):
    return ('<?xml version="1.0" encoding="utf-8"?><Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">'
            + time_series(points, f"<in_Domain.mRID>{target}</in_Domain.mRID><out_Domain.mRID>{origin}</out_Domain.mRID>")
            + "</Publication_MarketDocument>").encode("utf-8")


LU, DE, BE = "10YLU-CEGEDEL-NQ", "10Y1001A1001A83F", "10YBE----------2"


class ElectricityGridTests(AllowlistAssertions, unittest.TestCase):
    def responses(self):
        flows = dataset("flows", [
            ("xml", "https://download.data.public.lu/f/cross-border-flow-de-lu-20261003.xml", "Germany > Luxembourg", "2026-10-04T07:50:35+00:00"),
            ("xml", "https://download.data.public.lu/f/cross-border-flow-lu-be-20261003.xml", "Luxembourg > Belgium", "2026-10-04T07:50:27+00:00"),
            ("xml", "https://download.data.public.lu/f/cross-border-flow-de-lu-20261002.xml", "Germany > Luxembourg", "2026-10-03T07:50:35+00:00"),
        ])
        return [
            dataset("load", [("xml", "https://download.data.public.lu/l/total-load-20261003.xml", "Total load - 3 October 2026", "2026-10-04T07:50:06+00:00")]),
            gl_document(time_series([(1, 400), (2, 500), (3, 600), (4, 500)])),
            dataset("generation", [("xml", "https://download.data.public.lu/g/actual-generation-20261003.xml", "Generation", "2026-10-04T07:50:17+00:00")]),
            gl_document(time_series([(1, 0), (2, 40), (4, 80)], "<MktPSRType><psrType>B16</psrType></MktPSRType>"),
                        time_series([(1, 10), (2, 10), (3, 10), (4, 10)], "<MktPSRType><psrType>B99</psrType></MktPSRType>"),
                        time_series([], "<MktPSRType><psrType>B19</psrType></MktPSRType>")),
            flows,
            flow_document(DE, LU, [(1, 300), (2, 300), (3, 300), (4, 300)]),
            flow_document(LU, BE, [(1, 40), (2, 0), (3, 0), (4, 0)]),
        ]

    def test_summarises_load_generation_and_latest_day_flows(self):
        http = ScriptedHttp(self.responses())
        result = LuxembourgData(http).get_electricity_grid()
        self.assertEqual(result["day"], "Total load - 3 October 2026")
        self.assertEqual(result["load"], {"average_mw": 500.0, "peak_mw": 600, "peak_at": "2026-10-02T22:30:00Z", "min_mw": 400,
                                          "energy_mwh": 500.0, "from": "2026-10-02T22:00:00Z", "until": "2026-10-02T22:45:00Z"})
        solar, unknown = result["generation"]
        # Position 3 is missing, so curveType A03 carries 40 MW forward: (0+40+40+80)/4 MW over one hour.
        self.assertEqual((solar["type"], solar["code"], solar["energy_mwh"], solar["peak_mw"]), ("Solar", "B16", 40.0, 80))
        self.assertEqual((unknown["type"], unknown["energy_mwh"]), ("B99", 10.0))
        self.assertEqual(result["generation_mwh"], 50.0)
        self.assertEqual(result["cross_border_flows"], [
            {"from": "Germany", "to": "Luxembourg", "energy_mwh": 300.0, "average_mw": 300.0},
            {"from": "Luxembourg", "to": "Belgium", "energy_mwh": 10.0, "average_mw": 10.0},
        ])
        self.assertEqual(result["net_import_mwh"], 290.0)
        self.assertNotIn("series", result)
        fetched = [call["url"] for call in http.calls]
        self.assertNotIn("https://download.data.public.lu/f/cross-border-flow-de-lu-20261002.xml", fetched)
        self.assertDownloadsAllowlisted(http)

    def test_flow_downloads_are_capped(self):
        responses = self.responses()
        responses[4] = dataset("flows", [("xml", f"https://download.data.public.lu/f/flow-{index}-20261003.xml", "flow", "2026-10-04")
                                         for index in range(12)])
        responses[5:] = [flow_document(DE, LU, [(1, 1)])] * 8
        http = ScriptedHttp(responses)
        result = LuxembourgData(http).get_electricity_grid()
        self.assertEqual(len(result["cross_border_flows"]), 8)
        self.assertEqual(http.responses, [])

    def test_include_series_adds_quarter_hour_points(self):
        result = LuxembourgData(ScriptedHttp(self.responses())).get_electricity_grid(include_series=True)
        self.assertEqual(result["series"]["load"][2], {"start": "2026-10-02T22:30:00Z", "mw": 600})
        self.assertEqual([point["mw"] for point in result["series"]["generation"]["Solar"]], [0, 40, 40, 80])

    def test_load_without_points_is_an_upstream_error(self):
        responses = self.responses()
        responses[1] = gl_document(time_series([]))
        with self.assertRaises(UpstreamError):
            LuxembourgData(ScriptedHttp(responses)).get_electricity_grid()

    def test_entsoe_points_ignore_bogus_positions_and_follow_resolution(self):
        series = ElementTree.fromstring(time_series([(1, 5), (2, "x"), (999999999, 7)], resolution="PT60M"))
        self.assertEqual(_entsoe_points(series, "quantity"), (60, [("2026-10-02T22:00:00Z", 5)]))
        self.assertEqual(_entsoe_points(ElementTree.fromstring("<TimeSeries/>"), "quantity"), (60, []))


ADEM_FILES = {
    "de-dispo-age.csv": (
        "Date,Genre,Age,Personnes\n"
        '"31-08-2026","F","20 - 24","100"\n"31-08-2026","M","20 - 24","150"\n"31-08-2026","I","20 - 24","1"\n'
        '"31-07-2026","F","20 - 24","90"\n"31-07-2026","M","20 - 24","140"\n"bad-date","M","20 - 24","999"\n'),
    "offres-series.csv": (
        "Date,Nature_contrat,Postes_declares,Stock_postes_vacants\n"
        '"31-08-2026","Emploi","2507","6267"\n"31-08-2026","Mesure","146","123"\n"31-08-2026","Interim","115","325"\n'),
    "de-dispo-commune.csv": (
        "Date,Commune,Canton,Sexe,Personnes\n"
        '"31-08-2026","Esch-Sur-Alzette","Esch-sur-Alzette","Femmes","909"\n'
        '"31-08-2026","Esch-Sur-Alzette","Esch-sur-Alzette","Hommes","994"\n'
        '"31-07-2026","Esch-Sur-Alzette","Esch-sur-Alzette","Femmes","901"\n'
        '"31-08-2026","Esch-Sur-Sure","Wiltz","Femmes","35"\n'
        '"31-08-2026","Bech","Echternach","Hommes","12"\n'),
}


class UnemploymentTests(AllowlistAssertions, unittest.TestCase):
    @staticmethod
    def metadata():
        return dataset("adem", [
            ("csv", "https://download.data.public.lu/a/datasc-skills-vacancies.csv", "microdata", "2026-07-20"),
            ("csv", "https://download.data.public.lu/a/offres-details.csv", "details", "2026-09-21"),
            *[("csv", f"https://download.data.public.lu/a/{name}", name, "2026-09-21") for name in ADEM_FILES],
        ])

    def http_for(self, *names):
        return ScriptedHttp([self.metadata(), *[ADEM_FILES[name].encode("utf-8") for name in names]])

    def test_national_series_combines_jobseekers_and_vacancies(self):
        http = self.http_for("de-dispo-age.csv", "offres-series.csv")
        result = LuxembourgData(http).get_unemployment()
        self.assertEqual((result["as_of"], result["count"]), ("2026-08", 2))
        latest, previous = result["months"]
        self.assertEqual(latest, {"month": "2026-08", "resident_jobseekers": 251, "women": 100, "men": 150,
                                  "new_vacancies": 2507, "open_vacancies": 6267})
        self.assertEqual((previous["resident_jobseekers"], previous["new_vacancies"]), (230, None))
        fetched = [call["url"] for call in http.calls[1:]]
        self.assertEqual(fetched, ["https://download.data.public.lu/a/de-dispo-age.csv", "https://download.data.public.lu/a/offres-series.csv"])
        self.assertDownloadsAllowlisted(http)

    def test_months_limits_the_series(self):
        result = LuxembourgData(self.http_for("de-dispo-age.csv", "offres-series.csv")).get_unemployment(months=1)
        self.assertEqual([item["month"] for item in result["months"]], ["2026-08"])

    def test_commune_matches_exactly_regardless_of_case(self):
        result = LuxembourgData(self.http_for("de-dispo-commune.csv")).get_unemployment(commune="esch-sur-alzette")
        self.assertEqual(result["commune"], "Esch-Sur-Alzette")
        self.assertEqual(result["months"], [
            {"month": "2026-08", "resident_jobseekers": 1903, "women": 909, "men": 994},
            {"month": "2026-07", "resident_jobseekers": 901, "women": 901, "men": 0},
        ])

    def test_commune_unique_substring_is_accepted(self):
        result = LuxembourgData(self.http_for("de-dispo-commune.csv")).get_unemployment(commune="bech")
        self.assertEqual((result["commune"], result["months"][0]["men"]), ("Bech", 12))

    def test_ambiguous_or_unknown_commune_is_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "ambiguous.*Esch-Sur-Alzette, Esch-Sur-Sure"):
            LuxembourgData(self.http_for("de-dispo-commune.csv")).get_unemployment(commune="Esch")
        with self.assertRaisesRegex(ValueError, "unknown"):
            LuxembourgData(self.http_for("de-dispo-commune.csv")).get_unemployment(commune="Atlantis")

    def test_missing_resource_is_an_upstream_error(self):
        metadata = dataset("adem", [("csv", "https://download.data.public.lu/a/other.csv", "other", "2026-09-21")])
        with self.assertRaises(UpstreamError):
            LuxembourgData(ScriptedHttp([metadata])).get_unemployment()


if __name__ == "__main__":
    unittest.main()
