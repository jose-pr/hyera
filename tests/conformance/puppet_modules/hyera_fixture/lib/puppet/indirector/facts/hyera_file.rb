# frozen_string_literal: true

require 'puppet/node/facts'
require 'puppet/indirector/code'
require 'puppet/util/yaml'

# The facts of the node are the mapping in ./facts.yaml, exactly as
# `puppet lookup --facts ./facts.yaml` reads them, and nothing else.
class Puppet::Node::Facts::HyeraFile < Puppet::Indirector::Code
  desc 'Facts from ./facts.yaml'

  def find(request)
    values = Puppet::Util::Yaml.safe_load_file('./facts.yaml', [Symbol])
    Puppet::Node::Facts.new(request.key, values)
  end
end
