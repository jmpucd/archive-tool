"""Box operations for the share poller: rclone file copy + JWT/as-user collaboration API.

Isolated from the poller's control flow so the SDK import stays lazy (the laptop
orchestrator never imports it) and so the box-sdk-gen specifics live in one place. Every
public function raises BoxOpsError with a plain message on failure.

Auth model: the JWT app authenticates as its enterprise service account, then acts *as*
John (`with_user_subject`) so it operates on the files rclone uploaded into John's Box.
Requires the app to be authorized in the UC Davis Box admin console first.
"""

import subprocess

from archive_tool.config import BoxConfig


class BoxOpsError(Exception):
    pass


def rclone_copy(source_path: str, remote: str, dest: str) -> str:
    """Copy a local (CentOS) directory into Box via rclone. Returns the full Box target.

    `rclone copy` puts the *contents* of source_path into dest, creating dest as needed.
    """
    target = f"{remote}{dest}"
    result = subprocess.run(
        ["rclone", "copy", source_path, target, "--create-empty-src-dirs"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BoxOpsError(f"rclone copy to {target} failed: {result.stderr.strip()}")
    return target


def client_as_user(box: BoxConfig):
    """Build a BoxClient authenticated via JWT and acting as `box.user_email`."""
    try:
        from box_sdk_gen import BoxClient, BoxJWTAuth, JWTConfig
    except ImportError as e:
        raise BoxOpsError(
            "box-sdk-gen not installed; run `uv sync --extra poller` on CentOS"
        ) from e

    jwt = JWTConfig.from_config_file(str(box.jwt_config_path))
    auth = BoxJWTAuth(config=jwt)
    user_id = _resolve_user_id(BoxClient(auth=auth), box.user_email)
    return BoxClient(auth=auth.with_user_subject(user_id))


def resolve_folder_id(client, box_path: str) -> str:
    """Resolve a Box folder path (relative to the acting user's root) to its folder ID."""
    folder_id = "0"  # "All Files" root of the acting user
    for part in [p for p in box_path.split("/") if p]:
        items = client.folders.get_folder_items(folder_id)
        match = next(
            (e for e in items.entries if _entry_type(e) == "folder" and e.name == part),
            None,
        )
        if match is None:
            raise BoxOpsError(f"Box folder not found: '{part}' in path '{box_path}'")
        folder_id = match.id
    return folder_id


def add_collaborators(client, folder_id: str, emails: list[str], role: str) -> None:
    """Invite each email as a collaborator on the folder with the given Box role."""
    from box_sdk_gen import (
        CreateCollaborationAccessibleBy,
        CreateCollaborationAccessibleByTypeField,
        CreateCollaborationItem,
        CreateCollaborationItemTypeField,
        CreateCollaborationRole,
    )

    for email in emails:
        try:
            client.user_collaborations.create_collaboration(
                item=CreateCollaborationItem(
                    type=CreateCollaborationItemTypeField.FOLDER, id=folder_id
                ),
                accessible_by=CreateCollaborationAccessibleBy(
                    type=CreateCollaborationAccessibleByTypeField.USER, login=email
                ),
                role=CreateCollaborationRole(role),
                notify=True,
            )
        except Exception as e:  # SDK raises BoxAPIError and friends
            raise BoxOpsError(f"could not add collaborator {email}: {e}") from e


def remove_collaborators(client, folder_id: str) -> int:
    """Remove all collaborations on a folder (leaves the files). Returns count removed."""
    try:
        collabs = client.list_collaborations.get_folder_collaborations(folder_id)
        removed = 0
        for c in collabs.entries or []:
            client.user_collaborations.delete_collaboration_by_id(c.id)
            removed += 1
        return removed
    except Exception as e:
        raise BoxOpsError(f"could not remove collaborators on folder {folder_id}: {e}") from e


def _resolve_user_id(admin_client, email: str) -> str:
    """Look up a managed user's ID by email (needs the 'Manage users' scope)."""
    try:
        users = admin_client.users.get_users(filter_term=email)
    except Exception as e:
        raise BoxOpsError(f"Box user lookup for {email} failed: {e}") from e
    entries = users.entries or []
    if not entries:
        raise BoxOpsError(f"no Box user found for {email}")
    return entries[0].id


def _entry_type(entry) -> str:
    """box-sdk-gen item .type is a str-valued enum; normalize to a plain string."""
    t = getattr(entry, "type", None)
    return getattr(t, "value", t)
