import concurrent.futures
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePath
from tempfile import TemporaryFile
from zipfile import ZipFile

import click
import requests
from tqdm import tqdm

from picomc.cli.utils import pass_instance_manager, pass_launcher
from picomc.downloader import DownloadQueue
from picomc.logging import logger
from picomc.mod import forge
from picomc.utils import Directory, die, sanitize_name

FORGE_PREFIX = "forge-"
ADDON_URL = "https://addons-ecs.forgesvc.net/api/v2/addon"
GETINFO_URL = "https://addons-ecs.forgesvc.net/api/v2/addon/{}/file/{}"
GETURL_URL = GETINFO_URL + "/download-url"


def resolve_packurl(path):
    if path.startswith("https://") and path.endswith(".zip"):
        return path
    regex = r"^(https:\/\/|twitch:\/\/)www\.curseforge\.com\/minecraft\/modpacks\/[-a-z0-9]+\/(download|download-client|files)\/(\d+)(\/file|\?client=y|)$"
    match = re.match(regex, path)
    if match:
        file_id = match.group(3)
        headers = {"User-Agent": "curl"}
        resp = requests.get(GETURL_URL.format("anything", file_id), headers=headers)
        resp.raise_for_status()
        return resp.text
    else:
        raise ValueError("Unsupported URL")


def install_from_zip(zipfileobj, launcher, instance_manager, instance_name=None):
    with ZipFile(zipfileobj) as pack_zf:
        with pack_zf.open("manifest.json") as fd:
            manifest = json.load(fd)

        assert manifest["manifestType"] == "minecraftModpack"
        assert manifest["manifestVersion"] == 1

        assert len(manifest["minecraft"]["modLoaders"]) == 1
        forge_ver = manifest["minecraft"]["modLoaders"][0]["id"]

        assert forge_ver.startswith(FORGE_PREFIX)
        forge_ver = forge_ver[len(FORGE_PREFIX) :]
        packname = manifest["name"]
        packver = manifest["version"]
        if instance_name is None:
            instance_name = "{}-{}".format(
                sanitize_name(packname), sanitize_name(packver)
            )
            logger.info(f"Installing {packname} version {packver}")
        else:
            logger.info(
                f"Installing {packname} version {packver} as instance {instance_name}"
            )

        if instance_manager.exists(instance_name):
            die("Instace {} already exists".format(instance_name))

        try:
            forge.install(
                versions_root=launcher.get_path(Directory.VERSIONS),
                libraries_root=launcher.get_path(Directory.LIBRARIES),
                forge_version=forge_ver,
            )
        except forge.AlreadyInstalledError:
            pass

        # Trusting the game version from the manifest may be a bad idea
        inst = instance_manager.create(
            instance_name,
            "{}-forge-{}".format(manifest["minecraft"]["version"], forge_ver),
        )
        # This is a random guess, but better than the vanilla 1G
        inst.config["java.memory.max"] = "4G"
        inst_dir = Path(inst.get_relpath())

        overrides = manifest["overrides"]
        mcdir: Path = inst_dir / "minecraft"
        for fileinfo in pack_zf.infolist():
            if fileinfo.is_dir():
                continue
            fname = fileinfo.filename
            try:
                outpath = mcdir / PurePath(fname).relative_to(overrides)
            except ValueError:
                continue
            if not outpath.parent.exists():
                outpath.parent.mkdir(parents=True, exist_ok=True)
            with pack_zf.open(fname) as infile, open(outpath, "wb") as outfile:
                shutil.copyfileobj(infile, outfile)

        project_files = {mod["projectID"]: mod["fileID"] for mod in manifest["files"]}
        headers = {"User-Agent": "curl"}
        dq = DownloadQueue()

        logger.info("Retrieving mod metadata from curse")
        modcount = len(project_files)
        moddir = mcdir / "mods"
        with tqdm(total=modcount) as tq:
            # Try to get as many file_infos as we can in one request
            # This endpoint only provides a few "latest" files for each project,
            # so it's not guaranteed that the response will contain the fileID
            # we are looking for. It's a gamble, but usually worth it in terms
            # of request count. The time benefit is not that great, as the endpoint
            # is slow.
            resp = requests.post(
                ADDON_URL, json=list(project_files.keys()), headers=headers
            )
            resp.raise_for_status()
            projects_meta = resp.json()
            for proj in projects_meta:
                proj_id = proj["id"]
                want_file = project_files[proj_id]
                for file_info in proj["latestFiles"]:
                    if want_file == file_info["id"]:
                        dq.add(
                            file_info["downloadUrl"],
                            moddir / file_info["fileName"],
                            size=file_info["fileLength"],
                        )
                        del project_files[proj_id]

            batch_recvd = modcount - len(project_files)
            logger.debug("Got {} batched".format(batch_recvd))
            tq.update(batch_recvd)

            with ThreadPoolExecutor(max_workers=16) as tpe:

                def dl(pid, fid):
                    resp = requests.get(GETINFO_URL.format(pid, fid), headers=headers)
                    resp.raise_for_status()
                    file_info = resp.json()
                    assert file_info["id"] == fid
                    dq.add(
                        file_info["downloadUrl"],
                        moddir / file_info["fileName"],
                        size=file_info["fileLength"],
                    )

                # Get remaining individually
                futmap = {}
                for pid, fid in project_files.items():
                    fut = tpe.submit(dl, pid, fid)
                    futmap[fut] = (pid, fid)

                for fut in concurrent.futures.as_completed(futmap.keys()):
                    try:
                        fut.result()
                    except Exception as ex:
                        pid, fid = futmap[fut]
                        logger.error(
                            "Could not get metadata for {}/{}: {}".format(pid, fid, ex)
                        )
                    else:
                        tq.update(1)

        logger.info("Downloading mod jars")
        dq.download()
        logger.info("Done installing {}".format(instance_name))


def install_from_path(path, launcher, instance_manager, instance_name=None):
    if os.path.exists(path):
        zipfile = ZipFile(path)
        with open(zipfile, "rb") as fd:
            install_from_zip(fd, launcher, instance_manager, instance_name)
    else:
        zipurl = resolve_packurl(path)
        with requests.get(zipurl, stream=True) as r:
            r.raise_for_status()
            with TemporaryFile() as tempfile:
                for chunk in r.iter_content(chunk_size=8192):
                    tempfile.write(chunk)
                install_from_zip(tempfile, launcher, instance_manager, instance_name)


@click.group("curse")
def curse_cli():
    """Handles modpacks from curseforge.com"""
    pass


@curse_cli.command("install")
@click.argument("path")
@click.option("--name", "-n", default=None, help="Name of the resulting instance")
@pass_instance_manager
@pass_launcher
def install_cli(launcher, im, path, name):
    """Install a modpack.

    An instance is created with the correct version of forge selected and all
    the mods from the pack installed.

    PATH can be a URL of the modpack (either twitch:// or https://
    containing a numeric identifier of the file) or a path to the curse zip file."""
    install_from_path(path, launcher, im, name)


def register_cli(root):
    root.add_command(curse_cli)
