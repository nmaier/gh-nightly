#!/usr/bin/env python
import os
import json
import subprocess
import sys
import re

from datetime import datetime
from hashlib import sha256
from io import BytesIO
from time import sleep
from types import MethodType
from xml.dom.minidom import parse as _XML
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import requests

from path import path


__version__ = "0.2"
USER_AGENT = "gh-nightly/{__version__} like cURL"

def XML(source):
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.unlink()

    rv = _XML(source)
    rv.__enter__ = MethodType(__enter__, rv)
    rv.__exit__ = MethodType(__exit__, rv)
    return rv


def call(*args, **kw):
    """ Print and call command """

    print "calling", args, kw
    return subprocess.check_output(*args, **kw)


def pull(owner, repo, target=None):
    """ Pull (or clone) repo """

    target = target or repo
    try:
        call(["git", "clone",
              "https://github.com/{owner}/{repo}".format(owner=owner,
                                                         repo=repo),
              target])
    except:
        with path(target):
            call(["git", "reset", "--hard"])
            call(["git", "remote", "update"])
            call(["git", "checkout", "master"])
            call(["git", "reset", "--hard"])
            call(["git", "pull"])


def update_installrdf(source, updateurl, version, updaterdf):
    """ Update the install.rdf, setting up updateurl and name, version """

    with XML(source) as rdf:
        # Update the extid
        un = updaterdf.getElementsByTagName("RDF:Description")[0]
        idn = rdf.getElementsByTagName("em:id")[0].firstChild
        un.setAttribute("about", "urn:mozilla:extension:{}".format(idn.data))

        # Update the version
        vn = rdf.getElementsByTagName("em:version")[0].firstChild
        vn.data = "{}{}".format(vn.data, version)
        name = rdf.getElementsByTagName("em:name")[0].firstChild
        un = updaterdf.getElementsByTagName("em:version")[0].firstChild
        un.data = vn.data

        # Set up update.rdf target application
        un = un.parentNode.parentNode
        for n in rdf.getElementsByTagName("em:targetApplication"):
            nn = n.cloneNode(True)
            for nd in nn.getElementsByTagName("Description"):
                nd.tagName = "RDF:Description"
            un.appendChild(nn)

        # Get the update info in order
        for n in rdf.getElementsByTagName("em:updateKey"):
            n.parentNode.removeChild(n)
        try:
            n = rdf.getElementsByTagName("em:updateURL")[0]
            while n.firstChild:
                n.removeChild(n.firstChild)
        except:
            n = rdf.createElement("em:updateURL")
            idn.parentNode.parentNode.appendChild(n)
        n.appendChild(rdf.createTextNode(updateurl))
        return BytesIO(rdf.toxml(encoding="utf-8"))


def pathkey(p):
    """ Generate a key for a path() for sorting """

    return (p.parent, p.name)


def make_xpi(updateurl, version, updaterdf):
    """ Create the XPI in memory """
    rv = BytesIO()
    with ZipFile(rv, "w", ZIP_DEFLATED) as zp:
        for f in sorted(path(".").walk("*.*"), key=pathkey):
            if f.name.endswith(".png"):
                zp.write(f, f[2:], compress_type=ZIP_STORED)
            elif f.name == "install.rdf":
                with update_installrdf(f, updateurl, version, updaterdf) as ip:
                    zp.writestr(f[2:], ip.getvalue())
            else:
                zp.write(f, f[2:])
    rv.seek(0, 0)
    return rv


def create_release(target, user, tag, tagmsg, payload):
    """ Create a release """
    s = requests.Session()
    s.verify = True
    s.auth = (user["name"], user["pass"])
    s.headers.update({"User-Agent": USER_AGENT})
    url = "https://api.github.com/repos/{owner}/{repo}/releases".format(
        **target)
    data = dict(tag_name=tag, name=tagmsg, body="Automated build")
    r = s.post(url, data=json.dumps(data),
               headers={"Content-Type": "application/json"})
    if r.status_code != 201:
        raise IOError("Failed to create release: {}".format(r.status_code))
    release = r.json()

    try:
        upload = "{name}-{tag}.xpi".format(tag=tag, **target)
        upload_url = "{url}?name={upload}".format(
            url=release["upload_url"].replace("{?name}", ""),
            upload=upload)
        download_url = (
            "https://github.com/{owner}/{repo}/releases/download/"
            "{tag}/{upload}"
            .format(upload=upload, tag=tag, **target))
        print upload_url, download_url
        # too bad: urllib3 does not correctly verify wildcard host names
        r = s.post(upload_url, data=payload, verify=False,
                   headers={"Content-Type": "application/x-xpinstall"})
        if r.status_code not in (201, 202):
            raise IOError("Failed to create asset: {}".format(r.status_code))
        return download_url

    except:
        # roll back release
        url = (
            "https://api.github.com/repos/{owner}/{repo}/releases/{id}"
            .format(id=release["id"], **target))
        s.delete(url)
        raise


def create(repo, target, user):
    """ Create a nightly build """

    try:
        with repo:
            call(["git", "describe", "--exact-match", "HEAD"])
        print >>sys.stderr, "Already got a corresponding tag"
        if "--force" not in sys.argv:
            return
    except:
        # No tag yet
        pass

    with repo:
        rev = call(["git", "rev-parse", "HEAD"])
    now = datetime.now()
    tag = now.strftime("nightly-%Y-%m-%d-%H%M")
    tagmsg = now.strftime("{fullname} nightly - %Y-%m-%d %H:%M".format(**target))
    version = now.strftime(".%Y%m%d.%H%M.{rev}".format(rev=rev[:8]))
    print tag, tagmsg, version

    updaterdf_file = path(__file__).parent / "update-nightly.rdf"
    with XML(updaterdf_file) as updaterdf:
        # create the XPI in memory
        with (repo / (target.get("subdir") or ".")):
            xpi = make_xpi(target["updateurl"], version, updaterdf)

        with repo:
            # create tag...
            try:
                call(["git", "tag", "-d", tag])
            except:
                pass
            call(["git", "tag", "-am", tagmsg, tag])
        try:
            with repo:
                # ... and push
                url = (
                    'https://{user}:{passwd}@github.com/{owner}/{repo}'
                    .format(user=user["name"], passwd=user["pass"], **target))
                call(["git", "push", "--mirror", "--force", url])
                # give github a moment to sync
                sleep(5)

            # create the release
            download_url = create_release(target, user, tag, tagmsg,
                                          xpi.getvalue())

            # finish up update.rdf
            hash = updaterdf.createElement("em:updateHash")
            sum = "sha256:{}".format(sha256(xpi.getvalue()).hexdigest())
            hash.appendChild(updaterdf.createTextNode(sum))
            link = updaterdf.createElement("em:updateLink")
            link.appendChild(updaterdf.createTextNode(download_url))

            for nt in updaterdf.getElementsByTagName("em:targetApplication"):
                for n in nt.getElementsByTagName("RDF:Description"):
                    n.appendChild(hash.cloneNode(True))
                    n.appendChild(link.cloneNode(True))
            with open(path(target["updaterdf"]).expand(), "wb") as op:
                op.write(updaterdf.toxml(encoding="utf-8"))

        except:
            # roll back tag
            with repo:
                call(["git", "tag", "-d", tag])
            raise

if __name__ == "__main__":
    import yaml
    with open("config.yml") as fp:
        config = yaml.safe_load(fp)
    base = config["base"]
    repo = path(base["repo"]).expand()
    target = config["target"]
    user = config["user"]

    if False:
        import logging
        import httplib
        httplib.HTTPConnection.debuglevel = 1
        logging.basicConfig()
        logging.getLogger().setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True

    # first get changes
    pull(base["owner"], base["repo"], repo)

    # push changes to clone
    create(repo, target, user)
