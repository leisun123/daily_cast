"""Self-hosted RSS 2.0 publication adapter with immutable media and atomic feed replacement."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree

from dailycast.core.hashes import sha256_bytes
from dailycast.db.models import Episode, Publication
from dailycast.publishing.contracts import FeedWriteResult, PublicAsset, RSSFeedItem

_ITUNES_NAMESPACE = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ElementTree.register_namespace("itunes", _ITUNES_NAMESPACE)


class RSSPublicationError(RuntimeError):
    """Raised when a local immutable asset or RSS document fails publication validation."""


@dataclass(frozen=True, slots=True)
class RSSSettings:
    """Configured channel identity and public origin for the one V1 self-hosted RSS target."""

    public_base_url: str
    feed_title: str
    feed_description: str
    language: str
    author: str

    @property
    def channel_url(self) -> str:
        """Return the canonical public page for the podcast channel."""
        return self.public_base_url.rstrip("/")

    @property
    def cover_url(self) -> str:
        """Return the immutable application-hosted podcast cover resource."""
        return f"{self.channel_url}/cover.png"


class RSSPublisher:
    """Publish V1 items to one local RSS feed while keeping public media paths immutable."""

    target_key = "local-rss"

    def __init__(self, *, data_dir: Path, public_dir: Path, settings: RSSSettings) -> None:
        self._data_dir = data_dir.resolve()
        self._public_dir = public_dir.resolve()
        self._settings = settings

    @property
    def feed_path(self) -> Path:
        """Return the sole public feed path rooted under configured PUBLIC_DIR."""
        return self._public_dir / "feed.xml"

    def promote_asset(self, episode: Episode) -> PublicAsset:
        """Copy a verified mutable draft to a content-addressed immutable public media path once."""
        if episode.draft_audio_path is None or episode.draft_audio_sha256 is None:
            raise RSSPublicationError("Episode has no verified draft audio")
        source = self._safe_data_path(episode.draft_audio_path)
        if not source.is_file():
            raise RSSPublicationError("Episode draft audio file does not exist")
        source_bytes = source.read_bytes()
        source_hash = sha256_bytes(source_bytes)
        if source_hash != episode.draft_audio_sha256:
            raise RSSPublicationError(
                "Episode draft audio checksum does not match Episode metadata"
            )
        asset_id = source_hash
        relative_path = (
            Path("media") / "episodes" / episode.public_id / f"{asset_id}.mp3"
        ).as_posix()
        target = self._safe_public_path(relative_path)
        reused = target.exists()
        if reused:
            self._verify_existing_asset(target, source_hash, len(source_bytes))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.part")
            try:
                shutil.copyfile(source, temporary)
                self._verify_existing_asset(temporary, source_hash, len(source_bytes))
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return PublicAsset(
            relative_path=relative_path,
            absolute_path=target,
            public_url=f"{self._settings.public_base_url.rstrip('/')}/{relative_path}",
            sha256=source_hash,
            byte_size=len(source_bytes),
            reused=reused,
        )

    def validate(self, episode: Episode, asset: PublicAsset) -> None:
        """Verify metadata, approval binding, and immutable asset identity before Feed inclusion."""
        if not episode.title or not episode.description or not episode.script_text:
            raise RSSPublicationError("Episode metadata and script must exist before publishing")
        if episode.actual_duration_ms is None or episode.actual_duration_ms <= 0:
            raise RSSPublicationError("Episode must have a positive merged audio duration")
        if episode.approved_script_revision != episode.script_revision:
            raise RSSPublicationError("Episode approved script revision is no longer current")
        if episode.approved_audio_version != episode.audio_version:
            raise RSSPublicationError("Episode approved audio version is no longer current")
        self._verify_existing_asset(asset.absolute_path, asset.sha256, asset.byte_size)

    def feed_item(
        self, episode: Episode, publication: Publication, asset: PublicAsset
    ) -> RSSFeedItem:
        """Project one immutable publication candidate without using title or date as its GUID."""
        if (
            episode.title is None
            or episode.description is None
            or episode.actual_duration_ms is None
        ):
            raise RSSPublicationError("Episode is incomplete for RSS item construction")
        return RSSFeedItem(
            guid=episode.public_id,
            title=episode.title,
            description=_episode_summary(episode),
            published_at=publication.created_at,
            duration_ms=episode.actual_duration_ms,
            asset=asset,
        )

    def publish(self, items: tuple[RSSFeedItem, ...]) -> FeedWriteResult:
        """Validate then atomically replace Feed XML with the caller-injected candidate."""
        by_guid = {item.guid: item for item in items}
        if len(by_guid) != len(items):
            raise RSSPublicationError("RSS feed cannot contain duplicate GUID values")
        ordered_items = tuple(
            sorted(by_guid.values(), key=lambda item: (item.published_at, item.guid), reverse=True)
        )
        for item in ordered_items:
            self._verify_existing_asset(
                item.asset.absolute_path, item.asset.sha256, item.asset.byte_size
            )
        document = self._build_document(ordered_items)
        temporary = self.feed_path.with_name("feed.xml.tmp")
        self.feed_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ElementTree.ElementTree(document).write(
                temporary,
                encoding="utf-8",
                xml_declaration=True,
            )
            payload = temporary.read_bytes()
            self._validate_document(payload, ordered_items)
            os.replace(temporary, self.feed_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return FeedWriteResult(
            feed_path=self.feed_path,
            feed_version=sha256_bytes(payload),
            item_count=len(ordered_items),
        )

    def reconcile(self, publication: Publication, episode: Episode) -> bool:
        """Verify Feed XML includes this exact asset before database state is finalized."""
        asset = self._asset_from_publication(publication)
        if asset is None or not self.feed_path.is_file():
            return False
        try:
            self._verify_existing_asset(asset.absolute_path, asset.sha256, asset.byte_size)
            root = ElementTree.fromstring(self.feed_path.read_bytes())
        except (ElementTree.ParseError, OSError, RSSPublicationError):
            return False
        if not self._channel_metadata_is_current(root):
            return False
        for item in root.findall("./channel/item"):
            enclosure = item.find("enclosure")
            if enclosure is None:
                continue
            if (
                item.findtext("guid") == episode.public_id
                and enclosure.attrib.get("url") == asset.public_url
                and enclosure.attrib.get("type") == asset.mime_type
                and enclosure.attrib.get("length") == str(asset.byte_size)
            ):
                return True
        return False

    def asset_from_publication(self, publication: Publication) -> PublicAsset | None:
        """Safely reconstruct a public asset for service-side Feed rebuilding."""
        return self._asset_from_publication(publication)

    def _build_document(self, items: tuple[RSSFeedItem, ...]) -> ElementTree.Element:
        """Build a standard RSS 2.0 tree with iTunes duration and author extension elements."""
        rss = ElementTree.Element("rss", {"version": "2.0"})
        channel = ElementTree.SubElement(rss, "channel")
        ElementTree.SubElement(channel, "title").text = self._settings.feed_title
        ElementTree.SubElement(channel, "link").text = self._settings.channel_url
        ElementTree.SubElement(channel, "description").text = self._settings.feed_description
        ElementTree.SubElement(channel, "language").text = self._settings.language
        ElementTree.SubElement(channel, "author").text = self._settings.author
        ElementTree.SubElement(channel, f"{{{_ITUNES_NAMESPACE}}}author").text = (
            self._settings.author
        )
        ElementTree.SubElement(
            channel,
            f"{{{_ITUNES_NAMESPACE}}}image",
            {"href": self._settings.cover_url},
        )
        for feed_item in items:
            item = ElementTree.SubElement(channel, "item")
            ElementTree.SubElement(item, "title").text = feed_item.title
            ElementTree.SubElement(item, "description").text = feed_item.description
            ElementTree.SubElement(item, "guid", {"isPermaLink": "false"}).text = feed_item.guid
            ElementTree.SubElement(item, "pubDate").text = _rfc822_datetime(feed_item.published_at)
            ElementTree.SubElement(
                item,
                "enclosure",
                {
                    "url": feed_item.asset.public_url,
                    "length": str(feed_item.asset.byte_size),
                    "type": feed_item.asset.mime_type,
                },
            )
            ElementTree.SubElement(item, f"{{{_ITUNES_NAMESPACE}}}duration").text = _duration(
                feed_item.duration_ms
            )
        return rss

    def _validate_document(self, payload: bytes, expected_items: tuple[RSSFeedItem, ...]) -> None:
        """Parse temporary XML and verify each immutable enclosure before promotion."""
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as error:
            raise RSSPublicationError("generated RSS XML is invalid") from error
        if root.tag != "rss" or root.attrib.get("version") != "2.0":
            raise RSSPublicationError("generated XML is not an RSS 2.0 document")
        if not self._channel_metadata_is_current(root):
            raise RSSPublicationError("RSS channel metadata validation failed")
        item_nodes = root.findall("./channel/item")
        actual_guids = [node.findtext("guid") for node in item_nodes]
        expected_guids = [item.guid for item in expected_items]
        if len(actual_guids) != len(set(actual_guids)) or set(actual_guids) != set(expected_guids):
            raise RSSPublicationError("RSS GUID validation failed")
        for expected in expected_items:
            matching = next(node for node in item_nodes if node.findtext("guid") == expected.guid)
            enclosure = matching.find("enclosure")
            if enclosure is None or enclosure.attrib != {
                "url": expected.asset.public_url,
                "length": str(expected.asset.byte_size),
                "type": expected.asset.mime_type,
            }:
                raise RSSPublicationError("RSS enclosure validation failed")

    def _channel_metadata_is_current(self, root: ElementTree.Element) -> bool:
        """Require the channel identity fields expected by podcast RSS importers."""
        channel = root.find("channel")
        if channel is None or channel.findtext("link") != self._settings.channel_url:
            return False
        cover = channel.find(f"{{{_ITUNES_NAMESPACE}}}image")
        return cover is not None and cover.attrib == {"href": self._settings.cover_url}

    def _asset_from_publication(self, publication: Publication) -> PublicAsset | None:
        """Reconstruct a public asset only when all durable publication fields are present."""
        if (
            publication.public_asset_path is None
            or publication.public_audio_url is None
            or publication.asset_sha256 is None
            or publication.asset_byte_size is None
        ):
            return None
        try:
            path = self._safe_public_path(publication.public_asset_path)
        except RSSPublicationError:
            return None
        return PublicAsset(
            relative_path=publication.public_asset_path,
            absolute_path=path,
            public_url=publication.public_audio_url,
            sha256=publication.asset_sha256,
            byte_size=publication.asset_byte_size,
        )

    def _safe_public_path(self, relative_path: str) -> Path:
        """Reject absolute paths and traversal before reading or creating public files."""
        candidate_relative = Path(relative_path)
        if candidate_relative.is_absolute() or not candidate_relative.parts:
            raise RSSPublicationError("public asset path must be relative")
        candidate = (self._public_dir / candidate_relative).resolve()
        try:
            candidate.relative_to(self._public_dir)
        except ValueError as error:
            raise RSSPublicationError("public asset path escapes configured PUBLIC_DIR") from error
        return candidate

    def _safe_data_path(self, relative_path: str) -> Path:
        """Reject private draft traversal before immutable public-asset promotion."""
        candidate_relative = Path(relative_path)
        if candidate_relative.is_absolute() or not candidate_relative.parts:
            raise RSSPublicationError("draft audio path must be relative")
        candidate = (self._data_dir / candidate_relative).resolve()
        try:
            candidate.relative_to(self._data_dir)
        except ValueError as error:
            raise RSSPublicationError("draft audio path escapes configured DATA_DIR") from error
        return candidate

    @staticmethod
    def _verify_existing_asset(path: Path, expected_sha256: str, expected_size: int) -> None:
        """Verify immutable media content before reuse, feed write, or recovery finalization."""
        if not path.is_file() or path.stat().st_size != expected_size:
            raise RSSPublicationError("public asset is missing or has unexpected size")
        if sha256_bytes(path.read_bytes()) != expected_sha256:
            raise RSSPublicationError("public asset checksum verification failed")


def _duration(duration_ms: int) -> str:
    """Render a podcast-friendly H:MM:SS duration without relying on locale-dependent formatting."""
    total_seconds = max(0, round(duration_ms / 1000))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


def _episode_summary(episode: Episode) -> str:
    """Keep the generated description as the RSS summary and append durable listening context."""
    if episode.description is None or episode.actual_duration_ms is None:
        raise RSSPublicationError("Episode is incomplete for RSS summary construction")
    return (
        f"{episode.description}\n\n"
        f"时长：{_duration(episode.actual_duration_ms)} · 话题数：{episode.news_count}"
    )


def _rfc822_datetime(value: datetime) -> str:
    """Format feed dates in UTC even when SQLite returned a naive timestamp during a local test."""
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return format_datetime(normalized.astimezone(UTC), usegmt=True)
