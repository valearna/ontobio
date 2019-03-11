import click
import json
import os
import yaml
import requests
import gzip
import urllib
import re
import glob

import yamldown

from functools import wraps

from ontobio.util.user_agent import get_user_agent
from ontobio.ontol_factory import OntologyFactory
from ontobio.io.gafparser import GafParser
from ontobio.io.assocwriter import GafWriter
from ontobio.io.assocwriter import GpadWriter
from ontobio.io import assocparser
from ontobio.io import gafgpibridge
from ontobio.io import entitywriter
from ontobio.rdfgen import assoc_rdfgen

from typing import Dict, Set

def thispath():
    os.path.normpath(os.path.abspath(__file__))

def gzips(file_function):

    @wraps(file_function)
    def wrapper(*args, **kwargs):
        output_file = file_function(*args, **kwargs)
        if isinstance(output_file, list):
            for o in output_file:
                zipup(o)
        else:
            zipup(output_file)

        return output_file

    return wrapper

def zipup(file_path):
    click.echo("Zipping {}".format(file_path))
    path, filename = os.path.split(file_path)
    zipname = "{}.gz".format(filename)
    target = os.path.join(path, zipname)

    with open(file_path, "rb") as p:
        with gzip.open(target, "wb") as tf:
            tf.write(p.read())

def find(l, finder):
    filtered = [n for n in l if finder(n)]
    if len(filtered) == 0:
        return None
    else:
        return filtered[0]

def metadata_file(metadata, group) -> Dict:
    metadata_yaml = os.path.join(metadata, "datasets", "{}.yaml".format(group))
    try:
        with open(metadata_yaml, "r") as group_data:
            click.echo("Found {group} metadata at {path}".format(group=group, path=metadata_yaml))
            return yaml.load(group_data, Loader=yaml.FullLoader)
    except Exception as e:
        raise click.ClickException("Could not find or read {}: {}".format(metadata_yaml, str(e)))

def gorule_title(metadata, rule_id) -> str:
    gorule_yamldown = os.path.join(metadata, "rules", "{}.md".format(rule_id))
    try:
        with open(gorule_yamldown, "r") as gorule_data:
            click.echo("Found {rule} at {path}".format(rule=rule_id, path=gorule_yamldown))
            return yamldown.load(gorule_data)[0]["title"]
    except Exception as e:
        raise click.ClickException("Could not find or read {}: {}".format(gorule_yamldown, str(e)))

def rule_id(rule_path) -> str:
    return os.path.splitext(os.path.basename(rule_path))[0]

def source_path(dataset_metadata, target_dir, group):
    extension = dataset_metadata["type"]
    if dataset_metadata["compression"]:
        extension = "{ext}.gz".format(ext=extension)

    path = os.path.join(target_dir, "groups", group, "{name}-src.{ext}".format(name=dataset_metadata["dataset"], ext=extension))
    return path


def download_source_gafs(group_metadata, target_dir, exclusions=[], base_download_url=None):
    """
    This looks at a group metadata dictionary and downloads each GAF source that is not in the exclusions list.
    For each downloaded file, keep track of the path of the file. If the file is zipped, it will unzip it here.
    This function returns a list of tuples of the dataset dictionary mapped to the downloaded source path.
    """
    gaf_urls = [ (data, data["source"]) for data in group_metadata["datasets"] if data["type"] == "gaf" and data["dataset"] not in exclusions ]
    # Map of dataset metadata to gaf download url

    click.echo("Found {}".format(", ".join( [ kv[0]["dataset"] for kv in gaf_urls ] )))
    downloaded_paths = []
    for dataset_metadata, gaf_url in gaf_urls:
        dataset = dataset_metadata["dataset"]
        # Local target download path setup - path and then directories
        path = source_path(dataset_metadata, target_dir, group_metadata["id"])
        os.makedirs(os.path.split(path)[0], exist_ok=True)

        click.echo("Downloading source gaf to {}".format(path))
        if urllib.parse.urlparse(gaf_url).scheme in ["ftp", "file"]:
            urllib.request.urlretrieve(gaf_url, path)
        else:
            is_relative = not urllib.parse.urlparse(gaf_url).path.startswith("/")
            if is_relative and base_download_url is not None:
                gaf_url = urllib.parse.urljoin(base_download_url, gaf_url)
            elif is_relative and base_download_url is None:
                # This is bad and we have to jump out`since we have no way of constructing a real gaf url
                raise click.ClickException("Option --base-download-url was not specified and the config url {} is a relative path.".format(gaf_url))

            response = requests.get(gaf_url, stream=True, headers={'User-Agent': get_user_agent(modules=[requests], caller_name=__name__)})
            content_length = int(response.headers.get("Content-Length", 0))

            with open(path, "wb") as downloaded:
                with click.progressbar(iterable=response.iter_content(chunk_size=512 * 1024), length=content_length, show_percent=True) as chunks:
                    for chunk in chunks:
                        if chunk:
                            downloaded.write(chunk)

        if dataset_metadata["compression"] == "gzip":
            # Unzip any downloaded file that has gzip, strip of the gzip extension
            unzipped = os.path.splitext(path)[0]
            unzip(path, unzipped)
            path = unzipped
        else:
            # otherwise file is coming in uncompressed. But we want to make sure
            # to zip up the original source also
            zipup(path)

        downloaded_paths.append((dataset_metadata, path))

    return downloaded_paths

def check_and_download_paint_source(paint_metadata, group_id, dataset, target_dir):
    paint_dataset = find(paint_metadata["datasets"], lambda d: d["dataset"] == "paint_{}".format(dataset))
    if paint_dataset is None:
        return None

    path = os.path.join(target_dir, "groups", group_id, "{}-src.gaf.gz".format(paint_dataset["dataset"]))
    click.echo("Downloading paint to {}".format(path))
    urllib.request.urlretrieve(paint_dataset["source"], path)
    unzipped = os.path.join(os.path.split(path)[0], "{}-src.gaf".format(paint_dataset["dataset"]))
    unzip(path, unzipped)
    return unzipped


def unzip(path, target):
    click.echo("Unzipping {}".format(path))
    def chunk_gen():
        with gzip.open(path, "rb") as p:
            while True:
                chunk = p.read(size=512 * 1024)
                if not chunk:
                    break
                yield chunk

    with open(target, "wb") as tf:
        with click.progressbar(iterable=chunk_gen()) as chunks:
            for chunk in chunks:
                tf.write(chunk)

def database_entities(metadata) -> Set[str]:
    dbxrefs_path = os.path.join(os.path.abspath(metadata), "db-xrefs.yaml")
    try:
        with open(dbxrefs_path, "r") as db_xrefs_file:
            click.echo("Found db-xrefs at {path}".format(path=dbxrefs_path))
            dbxrefs = yaml.load(db_xrefs_file, Loader=yaml.FullLoader)
    except Exception as e:
        raise click.ClickException("Could not find or read {}: {}".format(dbxrefs_path, str(e)))

    return set([entity["database"] for entity in dbxrefs])

def groups(metadata) -> Set[str]:
    groups_path = os.path.join(os.path.abspath(metadata), "groups.yaml")
    try:
        with open(groups_path, "r") as groups_file:
            click.echo("Found groups at {path}".format(path=groups_path))
            groups_list = yaml.load(groups_file, Loader=yaml.FullLoader)
    except Exception as e:
        raise click.ClickException("Could not find or read {}: {}".format(groups_path, str(e)))

    return set([group["shorthand"] for group in groups_list])

"""
Produce validated gaf using the gaf parser/
"""
@gzips
def produce_gaf(dataset, source_gaf, ontology_graph, gpipath=None, paint=False, group="unknown", rule_titles=None, db_entities=None, group_idspace=None):
    filtered_associations = open(os.path.join(os.path.split(source_gaf)[0], "{}_noiea.gaf".format(dataset)), "w")

    config = assocparser.AssocParserConfig(
        ontology=ontology_graph,
        filter_out_evidence=["IEA"],
        filtered_evidence_file=filtered_associations,
        gpi_authority_path=gpipath,
        paint=paint,
        rule_titles=rule_titles,
        entity_idspaces=db_entities,
        group_idspace=group_idspace
    )
    validated_gaf_path = os.path.join(os.path.split(source_gaf)[0], "{}_valid.gaf".format(dataset))
    outfile = open(validated_gaf_path, "w")
    gafwriter = GafWriter(file=outfile, source=dataset)

    click.echo("Validating source GAF: {}".format(source_gaf))
    parser = GafParser(config=config, group=group, dataset=dataset)
    with open(source_gaf) as sg:
        lines = sum(1 for line in sg)

    with open(source_gaf) as gaf:
        with click.progressbar(iterable=parser.association_generator(file=gaf), length=lines) as associations:
            for assoc in associations:
                gafwriter.write_assoc(assoc)

    outfile.close()
    filtered_associations.close()

    with open(os.path.join(os.path.split(source_gaf)[0], "{}.report.md".format(dataset)), "w") as report_md:
        report_md.write(parser.report.to_markdown())

    with open(os.path.join(os.path.split(source_gaf)[0], "{}.report.json".format(dataset)), "w") as report_json:
        report_json.write(json.dumps(parser.report.to_report_json(), indent=4))

    return [validated_gaf_path, filtered_associations.name]


@gzips
def make_products(dataset, target_dir, gaf_path, products, ontology_graph):
    gafparser = GafParser()
    gafparser.config = assocparser.AssocParserConfig(
        ontology=ontology_graph,
        paint=True
    )

    with open(gaf_path) as sg:
        lines = sum(1 for line in sg)

    product_files = {
        "gpad": open(os.path.join(os.path.split(gaf_path)[0], "{}.gpad".format(dataset)), "w"),
        "ttl": open(os.path.join(os.path.split(gaf_path)[0], "{}_cam.ttl".format(dataset)), "wb")
    }

    # def write_gpi_entity(association, bridge, gpiwriter):
    with open(gaf_path) as gf:
        # gpi info:
        click.echo("Using {} as the gaf to build data products with".format(gaf_path))
        if products["ttl"]:
            click.echo("Setting up {}".format(product_files["ttl"].name))
            rdf_writer = assoc_rdfgen.TurtleRdfWriter(label=os.path.split(product_files["ttl"].name)[1] )
            transformer = assoc_rdfgen.CamRdfTransform(writer=rdf_writer)
            parser_config = assocparser.AssocParserConfig(ontology=ontology_graph)

        if products["gpad"]:
            click.echo("Setting up {}".format(product_files["gpad"].name))
            gpadwriter = GpadWriter(file=product_files["gpad"])

        click.echo("Making products...")
        with click.progressbar(iterable=gafparser.association_generator(file=gf), length=lines) as associations:
            for association in associations:
                if products["ttl"]:
                    if "header" not in association or not association["header"]:
                        transformer.provenance()
                        transformer.translate(association)

                if products["gpad"]:
                    gpadwriter.write_assoc(association)

        # post ttl steps
        if products["ttl"]:
            click.echo("Writing ttl to disk")
            rdf_writer.serialize(destination=product_files["ttl"])

        # After we run through associations
        for f in product_files.values():
            f.close()

    return [product_files[prod].name for prod in sorted(product_files.keys()) if products[prod]]

@gzips
def produce_gpi(dataset, target_dir, gaf_path, ontology_graph):
    gafparser = GafParser()
    gafparser.config = assocparser.AssocParserConfig(
        ontology=ontology_graph
    )
    with open(gaf_path) as sg:
        lines = sum(1 for line in sg)

    gpi_path = os.path.join(os.path.split(gaf_path)[0], "{}.gpi".format(dataset))
    with open(gaf_path) as gf, open(gpi_path, "w") as gpi:
        click.echo("Using {} as the gaf to build gpi with".format(gaf_path))
        bridge = gafgpibridge.GafGpiBridge()
        gpiwriter = entitywriter.GpiWriter(file=gpi)
        gpi_cache = set()

        with click.progressbar(iterable=gafparser.association_generator(file=gf), length=lines) as associations:
            for association in associations:
                entity = bridge.convert_association(association)
                if entity not in gpi_cache and entity is not None:
                    # If the entity is not in the cache, add it and write it out
                    gpi_cache.add(entity)
                    gpiwriter.write_entity(entity)

    return gpi_path


@gzips
def produce_ttl(dataset, target_dir, gaf_path, ontology_graph):
    gafparser = GafParser()
    gafparser.config = assocparser.AssocParserConfig(
        ontology=ontology_graph
    )

    with open(gaf_path) as sg:
        lines = sum(1 for line in sg)

    ttl_path = os.path.join(os.path.split(gaf_path)[0], "{}_cam.ttl".format(dataset))
    click.echo("Producing ttl: {}".format(ttl_path))
    rdf_writer = assoc_rdfgen.TurtleRdfWriter()
    transformer = assoc_rdfgen.CamRdfTransform(writer=rdf_writer)
    parser_config = assocparser.AssocParserConfig(ontology=ontology_graph)

    with open(gaf_path) as gf:
        with click.progressbar(iterable=gafparser.association_generator(file=gf), length=lines) as associations:
            for association in associations:
                if "header" not in association or not association["header"]:
                    transformer.provenance()
                    transformer.translate(association)

    with open(ttl_path, "wb") as ttl:
        click.echo("Writing ttl to disk")
        rdf_writer.serialize(destination=ttl)

    return ttl_path

@gzips
def merge_mod_and_paint(mod_gaf_path, paint_gaf_path):

    def header_and_annotations(gaf_file):
        headers = []
        annotations = []

        for line in gaf_file.readlines():
            line = line.rstrip("\n")
            if line.startswith("!"):
                headers.append(line)
            else:
                annotations.append(line)

        return (headers, annotations)

    dirs, name = os.path.split(mod_gaf_path)
    merged_path = os.path.join(dirs, "{}.gaf".format(name.rsplit("_", maxsplit=1)[0]))
    click.echo("Merging paint into base annotations at {}".format(merged_path))
    with open(mod_gaf_path) as mod, open(paint_gaf_path) as paint:
        mod_header, mod_annotations = header_and_annotations(mod)
        paint_header, paint_annotations = header_and_annotations(paint)

        the_header = mod_header + \
            ["!=================================",
            "!",
            "!PAINT Header copied from {}".format(os.path.basename(paint_gaf_path)),
            "!================================="]
        the_header += paint_header[8:] + \
            ["!=================================",
            "!",
            "!Documentation about this header can be found here: https://github.com/geneontology/go-site/blob/master/docs/gaf_validation.md",
            "!"]

        all_lines = the_header + mod_annotations + paint_annotations

        with open(merged_path, "w") as merged_file:
            merged_file.write("\n".join(all_lines))

    return merged_path


@click.group()
def cli():
    pass

@cli.command()
@click.argument("group")
@click.option("--metadata", "-m", type=click.Path(), required=True)
@click.option("--gpad", default=False, is_flag=True)
@click.option("--ttl", default=False, is_flag=True)
@click.option("--target", "-t", type=click.Path(), required=True)
@click.option("--ontology", "-o", type=click.Path(exists=True), required=False)
@click.option("--exclude", "-x", multiple=True)
@click.option("--base-download-url", "-b", default=None)
def produce(group, metadata, gpad, ttl, target, ontology, exclude, base_download_url):

    products = {
        "gaf": True,
        "gpi": True,
        "gpad": gpad,
        "ttl": ttl
    }
    click.echo("Making products {}.".format(", ".join([key for key in products if products[key]])))
    absolute_target = os.path.abspath(target)
    os.makedirs(os.path.join(absolute_target, "groups"), exist_ok=True)
    click.echo("Products will go in {}".format(absolute_target))
    absolute_metadata = os.path.abspath(metadata)

    group_metadata = metadata_file(absolute_metadata, group)
    click.echo("Loading ontology: {}...".format(ontology))
    ontology_graph = OntologyFactory().create(ontology)

    downloaded_gaf_sources = download_source_gafs(group_metadata, absolute_target, exclusions=exclude, base_download_url=base_download_url)
    # dict of Dataset Metadata --> downloaded source paths (unzipped)
    # source_gafs = {zip_path: os.path.join(os.path.split(zip_path)[0], "{}-src.gaf".format(dataset["dataset"])) for dataset, zip_path in source_gaf_zips.items()}

    # extract the titles for the go rules, this is a dictionary comprehension
    rule_titles = {rule_id(rule_path): gorule_title(metadata, rule_id(rule_path))
        for rule_path in glob.glob("{}/*.md".format(os.path.join(metadata, "rules"))) if rule_id(rule_path) not in ["ABOUT", "README", "SOP"]}

    paint_metadata = metadata_file(absolute_metadata, "paint")
    db_entities = database_entities(absolute_metadata)
    group_ids = groups(absolute_metadata)

    for dataset_metadata, source_gaf in downloaded_gaf_sources:
        dataset = dataset_metadata["dataset"]
        # Set paint to True when the group is "paint".
        # This will prevent filtering of IBA (GO_RULE:26) when paint is being treated as a top level group, like for paint_other.
        valid_gaf = produce_gaf(dataset, source_gaf, ontology_graph, paint=(group=="paint"), group=group, rule_titles=rule_titles, db_entities=db_entities, group_idspace=group_ids)[0]

        gpi = produce_gpi(dataset, absolute_target, valid_gaf, ontology_graph)

        paint_src_gaf = check_and_download_paint_source(paint_metadata, group_metadata["id"], dataset, absolute_target)

        end_gaf = valid_gaf
        if paint_src_gaf is not None:
            paint_gaf = produce_gaf("paint_{}".format(dataset), paint_src_gaf, ontology_graph, gpipath=gpi, paint=True, group="paint")[0]
            end_gaf = merge_mod_and_paint(valid_gaf, paint_gaf)
        else:
            gafgz = "{}.gz".format(valid_gaf)
            os.rename(gafgz, os.path.join(os.path.split(gafgz)[0], "{}.gaf.gz".format(dataset)))

        make_products(dataset, absolute_target, end_gaf, products, ontology_graph)


@cli.command()
@click.argument("group")
@click.argument("dataset")
@click.option("--metadata", "-m", type=click.Path(), required=True)
@click.option("--target", type=click.Path(), required=True)
@click.option("--ontology", type=click.Path(), required=True)
def paint(group, dataset, metadata, target, ontology):
    absolute_metadata = os.path.abspath(metadata)
    absolute_target = os.path.abspath(target)
    os.makedirs(os.path.join(absolute_target, "groups"), exist_ok=True)
    paint_metadata = metadata_file(absolute_metadata, "paint")
    paint_src_gaf = check_and_download_paint_source(paint_metadata, dataset, absolute_target)

    click.echo("Loading ontology: {}...".format(ontology))
    ontology_graph = OntologyFactory().create(ontology)

    gpi_path = os.path.join(absolute_target, "groups", dataset, "{}.gpi".format(dataset))
    click.echo("Using GPI at {}".format(gpi_path))
    paint_gaf = produce_gaf("paint_{}".format(dataset), paint_src_gaf, ontology_graph, gpipath=gpi_path)


if __name__ == "__main__":
    cli()
