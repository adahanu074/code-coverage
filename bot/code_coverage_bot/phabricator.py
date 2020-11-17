# -*- coding: utf-8 -*-

import os
import re

import structlog
from libmozdata.phabricator import BuildState
from libmozdata.phabricator import PhabricatorAPI
from libmozdata.phabricator import PhabricatorRevisionNotFoundException

from code_coverage_bot import hgmo
from code_coverage_bot.secrets import secrets
from code_coverage_tools import COVERAGE_EXTENSIONS

logger = structlog.get_logger(__name__)

PHABRICATOR_REVISION_REGEX = re.compile(
    "Differential Revision: (https://phabricator.services.mozilla.com/D([0-9]+))"
)


def parse_revision_id(desc):
    match = PHABRICATOR_REVISION_REGEX.search(desc)
    if not match:
        return None
    return int(match.group(2))


def parse_revision_url(desc):
    match = PHABRICATOR_REVISION_REGEX.search(desc)
    if not match:
        return None
    return match.group(1)


class PhabricatorUploader(object):
    def __init__(self, repo_dir, revision):
        self.repo_dir = repo_dir
        self.revision = revision

        # Read third party exclusion lists from repo
        third_parties = os.path.join(
            self.repo_dir, "tools/rewriting/ThirdPartyPaths.txt"
        )
        if os.path.exists(third_parties):
            self.third_parties = [line.rstrip() for line in open(third_parties)]
        else:
            self.third_parties = []
            logger.warn("Missing third party exclusion list", path=third_parties)

    def _find_coverage(self, report, path):
        """
        Find coverage value in a covdir report
        """
        assert isinstance(report, dict)

        parts = path.split("/")
        for part in filter(None, parts):
            if part not in report["children"]:
                # Only send warning for non 3rd party + supported extensions
                if self.is_third_party(path):
                    logger.info("Path not found in report for third party", path=path)
                elif not self.is_supported_extension(path):
                    logger.info(
                        "Path not found in report for unsupported extension", path=path
                    )
                else:
                    logger.warn("Path not found in report", path=path)
                return
            report = report["children"][part]

        return report["coverage"]

    def _build_coverage_map(self, annotate, coverage_record):
        # We can't use plain line numbers to map coverage data from the build changeset to the
        # changeset of interest, indeed there could be intermediate changesets between them
        # modifying the same lines, thus displacing the line numbers.
        # In order to uniquely identify lines, and thus map coverage data, we use the annotate
        # data. The line number and changeset where a line was introduced are unique, so whenever
        # they match in the annotate data of the two changesets, we can be sure that it is the
        # same line.
        coverage_map = {}

        for data in annotate:
            # The line number at the build changeset.
            # Line numbers start from 1 in the annotate data, from 0 in the coverage data.
            lineno = data["lineno"] - 1
            # The line number when it was introduced.
            orig_line = data["targetline"]
            # The changeset when it was introduced.
            orig_changeset = data["node"]

            if lineno < len(coverage_record):
                key = (orig_changeset, orig_line)
                coverage_map[key] = coverage_record[lineno]

        return coverage_map

    def _apply_coverage_map(self, annotate, coverage_map):
        phab_coverage_data = ""

        for data in annotate:
            # The line number when it was introduced.
            orig_line = data["targetline"]
            # The changeset when it was introduced.
            orig_changeset = data["node"]

            key = (orig_changeset, orig_line)
            if key in coverage_map:
                count = coverage_map[key]
                if count == -1:
                    # A non-executable line.
                    phab_coverage_data += "N"
                elif count > 0:
                    phab_coverage_data += "C"
                else:
                    phab_coverage_data += "U"
            else:
                # We couldn't find the original changeset-original line in the annotate data for the build changeset,
                # this means that this line has been overwritten by another changeset.
                phab_coverage_data += "X"

        return phab_coverage_data

    def is_third_party(self, path):
        """
        Check a file against known list of third party paths
        """
        for third_party in self.third_parties:
            if path.startswith(third_party):
                return True
        return False

    def is_supported_extension(self, path):
        """
        Check a file has a supported extension
        """
        _, ext = os.path.splitext(path)
        if not ext:
            return False
        return ext[1:] in COVERAGE_EXTENSIONS

    def generate(self, report, changesets):
        results = {}

        with hgmo.HGMO(self.repo_dir) as hgmo_server:
            for changeset in changesets:
                # Retrieve the revision ID for this changeset.
                revision_id = parse_revision_id(changeset["desc"])
                if revision_id is None:
                    continue

                results[changeset["node"]] = {
                    "revision_id": revision_id,
                    "paths": {},
                }

                # For each file...
                for path in changeset["files"]:
                    # Retrieve the coverage data.
                    coverage_record = self._find_coverage(report, path)
                    if coverage_record is None:
                        continue

                    # Retrieve the annotate data for the build changeset.
                    build_annotate = hgmo_server.get_annotate(self.revision, path)
                    if build_annotate is None:
                        # This means the file has been removed by another changeset, but if this is the
                        # case, then we shouldn't have a coverage record and so we should have *continue*d
                        # earlier.
                        assert (
                            False
                        ), "Failure to retrieve annotate data for the build changeset"

                    # Build the coverage map from the annotate data and the coverage data of the build changeset.
                    coverage_map = self._build_coverage_map(
                        build_annotate, coverage_record
                    )

                    # Retrieve the annotate data for the changeset of interest.
                    annotate = hgmo_server.get_annotate(changeset["node"], path)
                    if annotate is None:
                        # This means the file has been removed by this changeset, and maybe was brought back by a following changeset.
                        continue

                    # List lines added by this patch
                    lines_added = [
                        line["lineno"] - 1
                        for line in annotate
                        if line["node"] == changeset["node"]
                    ]

                    # Apply the coverage map on the annotate data of the changeset of interest.
                    coverage = self._apply_coverage_map(annotate, coverage_map)

                    results[changeset["node"]]["paths"][path] = {
                        "lines_added": sum(
                            coverage[line] != "N"
                            for line in lines_added
                            if line < len(coverage)
                        ),
                        "lines_unknown": sum(
                            coverage[line] == "X"
                            for line in lines_added
                            if line < len(coverage)
                        ),
                        "lines_covered": sum(
                            coverage[line] == "C"
                            for line in lines_added
                            if line < len(coverage)
                        ),
                        "coverage": coverage,
                    }

        return results

    def upload(self, report, changesets):
        results = self.generate(report, changesets)

        if secrets[secrets.PHABRICATOR_ENABLED]:
            phabricator = PhabricatorAPI(
                secrets[secrets.PHABRICATOR_TOKEN], secrets[secrets.PHABRICATOR_URL]
            )
        else:
            phabricator = None

        for result in results.values():
            rev_id = result["revision_id"]

            # Only upload raw coverage data to Phabricator, not stats
            coverage = {path: cov["coverage"] for path, cov in result["paths"].items()}
            logger.info("{} coverage: {}".format(rev_id, coverage))

            if not phabricator or not coverage:
                continue

            try:
                rev_data = phabricator.load_revision(rev_id=rev_id)
                phabricator.upload_coverage_results(
                    rev_data["fields"]["diffPHID"], coverage
                )
                # XXX: This is only necessary until https://bugzilla.mozilla.org/show_bug.cgi?id=1487843 is resolved.
                phabricator.upload_lint_results(
                    rev_data["fields"]["diffPHID"], BuildState.Pass, []
                )
            except PhabricatorRevisionNotFoundException:
                logger.warn("Phabricator revision not found", rev_id=rev_id)

        return results
