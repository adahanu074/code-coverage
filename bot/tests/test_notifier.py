# -*- coding: utf-8 -*-
import hglib

from code_coverage_bot import hgmo
from code_coverage_bot.notifier import notify_email
from code_coverage_bot.phabricator import PhabricatorUploader
from conftest import add_file
from conftest import changesets
from conftest import commit
from conftest import copy_pushlog_database
from conftest import covdir_report


def test_notification(mock_secrets, mock_taskcluster, mock_phabricator, fake_hg_repo):
    hg, local, remote = fake_hg_repo

    add_file(hg, local, "file", "1\n2\n3\n4\n")
    revision1 = commit(hg, 1)

    add_file(hg, local, "file", "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n")
    revision2 = commit(hg, 2)

    hg.push(dest=bytes(remote, "ascii"))

    copy_pushlog_database(remote, local)

    with hgmo.HGMO(local) as hgmo_server:
        stack = changesets(hgmo_server, revision2)
    assert len(stack) == 2
    assert (
        stack[0]["desc"]
        == "Commit [(b'A', b'file')]Differential Revision: https://phabricator.services.mozilla.com/D1"
    )
    assert (
        stack[1]["desc"]
        == "Commit [(b'M', b'file')]Differential Revision: https://phabricator.services.mozilla.com/D2"
    )

    report = covdir_report(
        {
            "source_files": [
                {"name": "file", "coverage": [None, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0]}
            ]
        }
    )
    phab = PhabricatorUploader(local, revision2)
    with hglib.open(local) as hg:
        changesets_coverage = phab.generate(hg, report, stack)

    assert changesets_coverage == {
        revision1: {
            "revision_id": 1,
            "paths": {
                "file": {
                    "lines_added": 3,
                    "lines_covered": 2,
                    "lines_unknown": 0,
                    "coverage": "NCCU",
                }
            },
        },
        revision2: {
            "revision_id": 2,
            "paths": {
                "file": {
                    "lines_added": 6,
                    "lines_covered": 0,
                    "lines_unknown": 0,
                    "coverage": "NCCUUUUUUU",
                }
            },
        },
    }

    mail = notify_email(revision2, stack, changesets_coverage)
    assert (
        mail
        == """* [Commit [(b'M', b'file')]Differential Revision: https://phabricator.services.mozilla.com/D2](https://phabricator.services.mozilla.com/D2): 0 covered out of 6 added.\n"""
    )
