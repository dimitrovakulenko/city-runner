"""Closed municipal selectors for public OSM relation catalogs."""

from __future__ import annotations

from collections.abc import Iterable
import re


DEFAULT_PROFILE = "admin-levels-v1"
GB_PROFILE = "gb-local-authority-v1"
CA_PROFILE = "ca-municipal-v1"
US_PROFILE = "us-municipal-v1"
AZ_PROFILE = "az-municipal-v1"

CA_PROVINCES = (
    "CA-AB", "CA-BC", "CA-MB", "CA-NB", "CA-NL", "CA-NS", "CA-NT",
    "CA-NU", "CA-ON", "CA-PE", "CA-QC", "CA-SK", "CA-YT",
)
_CA_LEVELS = {
    "CA-AB": {6}, "CA-BC": {8}, "CA-MB": {6}, "CA-NB": {6},
    "CA-NL": {6}, "CA-NS": {6}, "CA-NT": {6}, "CA-NU": {6, 8},
    "CA-ON": {6, 8}, "CA-PE": {6}, "CA-QC": {8}, "CA-SK": {6},
    "CA-YT": {6},
}

_GB_PREFIX_LEVELS = {
    "E06": {6}, "E07": {8}, "E08": {8}, "E09": {6, 8},
    "W06": {6}, "S12": {6}, "N09": {6, 8},
}
_GB_CONTRADICTORY_DESIGNATIONS = {
    "civil_parish", "electoral_district", "electoral_ward", "metropolitan_county",
    "non_metropolitan_county", "parish", "ward",
}
_PROFILE_COUNTRIES = {
    "admin-levels-v1": None,
    "gb-local-authority-v1": "GB",
    "ca-municipal-v1": "CA",
    "us-municipal-v1": "US",
    "az-municipal-v1": "AZ",
}
_NONMUNICIPAL_PLACE_TYPES = {
    "borough", "county", "district", "electoral_area", "neighbourhood",
    "neighborhood", "quarter", "region", "suburb", "township", "ward",
}
_MUNICIPAL_PLACE_TYPES = {"city", "municipality", "town", "village"}
_US_EXPLICIT_NONMUNICIPAL_TYPES = _NONMUNICIPAL_PLACE_TYPES | {
    "unincorporated_place", "census_designated_place",
}


def country_selector_profile(country: str) -> str:
    return {"GB": GB_PROFILE, "CA": CA_PROFILE, "US": US_PROFILE, "AZ": AZ_PROFILE}.get(country, DEFAULT_PROFILE)


def profile_is_valid(country: str, profile: str) -> bool:
    if not isinstance(profile, str):
        return False
    profile_country = _PROFILE_COUNTRIES.get(profile)
    # This is a captured-profile registry: a later current profile can never
    # make a queued, immutable v1 task invalid or reinterpret its selector.
    return profile in _PROFILE_COUNTRIES and (profile_country is None or profile_country == country)


def default_levels(country: str) -> list[int]:
    if country == "GB":
        return [6, 8]
    if country == "CA":
        return [6, 8]
    if country == "US":
        return [4, 5, 8]
    if country == "AZ":
        return [8]
    raise KeyError(country)


def levels_allowed(country: str, levels: Iterable[int]) -> bool:
    requested = set(levels)
    if country == "AZ":
        return requested == {8}
    allowed = set(default_levels(country))
    return bool(requested) and requested <= allowed


def country_query(country: str, levels: list[int], profile: str) -> str | None:
    level_filter = "|".join(str(level) for level in levels)
    if profile == DEFAULT_PROFILE:
        return None
    if country == "GB" and profile == GB_PROFILE:
        return (
            f'[out:json][timeout:120][maxsize:134217728];area["ISO3166-1"="GB"]["admin_level"="2"]->.country;'
            f'.country out ids;rel(area.country)["boundary"="administrative"]["admin_level"~"^({level_filter})$"]'
            '["ref:gss"~"^(E0[6-9]|W06|S12|N09)[0-9]{6}$"];out tags bb;'
        )
    if country == "US" and profile == US_PROFILE:
        city_branches = []
        if 8 in levels:
            city_branches.append('rel(area.country)["boundary"="administrative"]["admin_level"="8"];')
        if 5 in levels:
            city_branches.append('rel(175905)["boundary"="administrative"]["admin_level"="5"]["wikidata"="Q60"]["border_type"="city"];')
        if 4 in levels:
            city_branches.append('rel(162069)["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"="US-DC"];')
        return (
            f'[out:json][timeout:120][maxsize:134217728];area["ISO3166-1"="US"]["admin_level"="2"]->.country;'
            f'.country out ids;({"".join(city_branches)});out tags bb;'
        )
    if country == "CA" and profile == CA_PROFILE:
        provinces = "|".join(scope[3:] for scope in CA_PROVINCES)
        return (
            f'[out:json][timeout:120][maxsize:134217728];area["ISO3166-1"="CA"]["admin_level"="2"]->.country;'
            '.country out ids;'
            f'rel(area.country)["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^CA-({provinces})$"]->.provinces;'
            'foreach.provinces->.province('
            '.province out tags;.province map_to_area->.province_area;.province_area out ids;'
            f'rel(area.province_area)["boundary"="administrative"]["admin_level"~"^({level_filter})$"]->.candidates;'
            '.candidates out body bb;'
            # `make` has no element context in Overpass QL; an untagged marker
            # closes this province's ordered candidate block. The preceding
            # province relation and area carry the public scope proof.
            'make scope;out;'
            ');'
        )
    if country == "AZ" and profile == AZ_PROFILE and levels == [8]:
        return (
            '[out:json][timeout:120][maxsize:134217728];area["ISO3166-1"="AZ"]["admin_level"="2"]->.country;'
            '.country out ids;rel(area.country)["boundary"="administrative"]["admin_level"="8"]'
            '["place"~"^(city|town|village|municipality)$"];out tags bb;'
        )
    return None


def relation_matches(
    country: str,
    profile: str,
    levels: list[int],
    tags: dict[str, str],
    *,
    identifier: int | None = None,
    selection_scope: str | None = None,
    members: Iterable[tuple[str, int, str]] | None = None,
) -> bool:
    try:
        level = int(tags.get("admin_level", ""))
    except (TypeError, ValueError):
        return False
    if tags.get("boundary") != "administrative" or level not in levels:
        return False
    if profile == DEFAULT_PROFILE:
        return True
    if profile == GB_PROFILE and country == "GB":
        reference = tags.get("ref:gss", "")
        if not isinstance(reference, str) or re.fullmatch(r"(E0[6-9]|W06|S12|N09)[0-9]{6}", reference) is None:
            return False
        code = reference[:3]
        allowed = _GB_PREFIX_LEVELS.get(code)
        if allowed is None or level not in allowed:
            return False
        designation = tags.get("designation")
        return designation is None or (isinstance(designation, str)
            and not (set(designation.split(";")) & _GB_CONTRADICTORY_DESIGNATIONS))
    if profile == US_PROFILE and country == "US":
        if level == 5:
            return identifier == 175905 and tags.get("wikidata") == "Q60" and tags.get("border_type") == "city"
        if level == 4:
            return identifier == 162069 and tags.get("ISO3166-2") == "US-DC"
        return level == 8 and not _us_has_nonmunicipal_type(tags)
    if profile == AZ_PROFILE and country == "AZ":
        return (selection_scope is None and levels == [8] and level == 8
                and tags.get("place") in _MUNICIPAL_PLACE_TYPES
                and not _has_nonmunicipal_type(tags, _NONMUNICIPAL_PLACE_TYPES))
    if profile == CA_PROFILE and country == "CA":
        if selection_scope not in _CA_LEVELS or level not in _CA_LEVELS[selection_scope]:
            return False
        if _has_nonmunicipal_type(tags, _NONMUNICIPAL_PLACE_TYPES):
            return False
        if selection_scope == "CA-BC" and level == 8:
            return members is not None and any(
                member_type == "node" and role == "admin_centre"
                for member_type, _member_id, role in members
            )
        place = tags.get("place")
        if selection_scope == "CA-NU" and level == 8:
            return place in _MUNICIPAL_PLACE_TYPES | {"hamlet"}
        if selection_scope in {"CA-QC", "CA-ON"}:
            return place in _MUNICIPAL_PLACE_TYPES
        return place in _MUNICIPAL_PLACE_TYPES
    return False


def _has_nonmunicipal_type(tags: dict[str, str], rejected: set[str]) -> bool:
    return any(set(tags[key].split(";")) & rejected for key in ("place", "border_type", "designation")
               if tags.get(key))


def _us_has_nonmunicipal_type(tags: dict[str, str]) -> bool:
    for key in ("place", "border_type", "designation"):
        value = tags.get(key)
        if not value:
            continue
        values = set(value.split(";"))
        # OSM models New York as a consolidated city-county; preserve that
        # explicit city signal while excluding county-only relations.
        if key == "border_type" and {"city", "county"} <= values:
            values.remove("county")
        if values & _US_EXPLICIT_NONMUNICIPAL_TYPES:
            return True
    return False


def validate_selection(country: str, profile: str, levels: list[int], selection_scope: str | None) -> bool:
    if not profile_is_valid(country, profile):
        return False
    if profile == DEFAULT_PROFILE:
        return selection_scope is None
    if not levels_allowed(country, levels):
        return False
    if country == "CA":
        return selection_scope is None or selection_scope in CA_PROVINCES
    if country == "AZ":
        return selection_scope is None and levels == [8]
    return selection_scope is None
