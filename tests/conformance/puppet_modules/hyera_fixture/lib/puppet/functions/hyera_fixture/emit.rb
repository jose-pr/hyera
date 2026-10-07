# frozen_string_literal: true

require 'json'

# Writes the value of a Puppet expression to standard output as one JSON document
# between two marker lines, converted by Puppet's own rich-data serialisation so a
# value reads the same on every Ruby.
Puppet::Functions.create_function(:'hyera_fixture::emit') do
  dispatch :emit do
    param 'Any', :value
  end

  def emit(value)
    data = Puppet::Pops::Serialization::ToDataConverter.convert(
      value,
      rich_data: true,
      symbol_as_string: false,
      local_reference: false,
      type_by_reference: true
    )
    $stdout.puts '@@HYERA-VALUE-BEGIN@@'
    $stdout.puts JSON.generate(data)
    $stdout.puts '@@HYERA-VALUE-END@@'
    nil
  end
end
