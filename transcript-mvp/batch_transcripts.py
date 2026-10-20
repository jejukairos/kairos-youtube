#!/usr/bin/env python3
"""Starter-compatible collector. No summary or sends by default."""
import sys

from kairos_transcripts.cli import main
from kairos_transcripts.collector import (
    classify_failure,
    collect_urls,
    extract_id,
    fetch_one,
)
from kairos_transcripts.common import timestamp

__all__ = ['classify_failure', 'collect_urls', 'extract_id', 'fetch_one', 'timestamp']
if __name__ == '__main__':
    raise SystemExit(main(['collect', *sys.argv[1:]]))
