# frozen_string_literal: true

# Merges each case's variants with Puppet's own MergeStrategy.
#
# usage: ruby oracle.rb CASES.jsonl OUT.jsonl
#
# One JSON object per line in, one per line out: {"id", "res"} with the merged
# value, {"id", "res": {"m": 1}} when nothing was found, or {"id", "err":
# [class, message]}. The value encoding is the one gen.py writes.
require 'puppet'
require 'json'

Puppet.initialize_settings([])
MISSING = Object.new

def decode(json)
  case json
  when Array then json.map { |x| decode(x) }
  when Hash
    return Float(json['f'].sub('inf', 'Infinity').sub('nan', 'NaN')) if json.key?('f')
    return MISSING if json.key?('m')

    out = {}
    json['h'].each { |k, v| out[decode(k)] = decode(v) }
    out
  else json
  end
end

def encode(value)
  case value
  when Float then { 'f' => value.to_s }
  when Array then value.map { |x| encode(x) }
  when Hash then { 'h' => value.map { |k, x| [encode(k), encode(x)] } }
  else value
  end
end

# The invocation object MergeStrategy#lookup reports to; it only needs these two.
class Invocation
  def with(*_args)
    yield
  end

  def report_result(result)
    result
  end
end

File.open(ARGV[1], 'w') do |out|
  File.foreach(ARGV[0], encoding: 'utf-8') do |line|
    kase = JSON.parse(line)
    result = { 'id' => kase['id'] }
    begin
      variants = kase['variants'].map { |v| decode(v) }
      strategy = Puppet::Pops::MergeStrategy.strategy(kase['spec'])
      found = true
      merged = catch(:no_such_key) do
        value = strategy.lookup(variants, Invocation.new) do |v|
          throw :no_such_key if v.equal?(MISSING)
          v
        end
        found = false
        value
      end
      result['res'] = found ? { 'm' => 1 } : encode(merged)
    rescue SystemStackError, StandardError => e
      result['err'] = [e.class.name, e.message]
    end
    out.puts JSON.generate(result)
  end
end
