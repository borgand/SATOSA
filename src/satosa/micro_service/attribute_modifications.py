import os

import yaml

from satosa.internal_data import DataConverter
from satosa.micro_service.service_base import ResponseMicroService


class AddStaticAttributes(ResponseMicroService):
    """
    Add static attributes to the responses.

    The path to the file describing the mapping (as YAML) of static attributes must be specified
    with the environment variable 'SATOSA_STATIC_ATTRIBUTES'.
    """
    def __init__(self, internal_attributes):
        super(AddStaticAttributes, self).__init__()
        self.data_converter = DataConverter(internal_attributes)

        mapping_file = os.environ.get("SATOSA_STATIC_ATTRIBUTES")
        if not mapping_file:
            raise ValueError("Could not find file containing mapping of static attributes.")

        with open(mapping_file) as f:
            self.static_attributes = yaml.safe_load(f)

    def process(self, context, data):
        all_attributes = data.get_attributes()
        all_attributes.update(self.data_converter.to_internal("saml", self.static_attributes))
        data.add_attributes(all_attributes)
        return data
