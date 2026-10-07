#!/usr/bin/env ruby
# frozen_string_literal: true

# Runs many `puppet lookup` queries in one Ruby process.
#
# usage: ruby drive_puppet.rb JOBS.json OUT.json [WORKERS]
#
# JOBS.json is a list of {"id", "dir", "argv", "render"?}: `dir` is the scenario
# directory as this machine sees it and `argv` the lookup arguments after the
# isolation and node settings. OUT.json gets one {"id", "rc", "stdout", "stderr",
# "lookup_error"?} per job, in order.
#
# Each job runs Puppet's own application class through Puppet::Util::CommandLine,
# as the `puppet` executable does, in a forked child so settings and caches are
# never shared between queries; forking after `require 'puppet'` only saves the
# library load. A spy on Puppet::Pops::Lookup.lookup records the class and message
# of a LookupError before re-raising it: the application swallows that error (exit
# 1, nothing printed), which a plain miss also does.
require 'json'
require 'fileutils'
require 'timeout'
require 'puppet'
require 'puppet/util/command_line'
require 'puppet/application/lookup'

module LookupSpy
  @depth = 0
  class << self; attr_accessor :depth; end

  def lookup(*args)
    LookupSpy.depth += 1
    begin
      super
    rescue Puppet::DataBinding::LookupError => e
      if LookupSpy.depth == 1 && ENV['DIFF_SPY']
        File.write(ENV['DIFF_SPY'], JSON.generate({ 'class' => e.class.name, 'message' => e.message }))
      end
      raise
    ensure
      LookupSpy.depth -= 1
    end
  end
end
Puppet::Pops::Lookup.singleton_class.prepend(LookupSpy)

NODE = 'golden.example.com'

def iso_args(root)
  ['--confdir', root + '/conf', '--codedir', root + '/code', '--vardir', root + '/var',
   '--logdir', root + '/log', '--rundir', root + '/run',
   '--environmentpath', './environments', '--basemodulepath', './modules']
end

def read_text(path)
  return '' unless File.exist?(path)

  File.read(path, mode: 'rb').force_encoding('UTF-8').scrub
end

def run_job(job, iso_base, idx)
  root = File.join(iso_base, idx.to_s)
  FileUtils.mkdir_p(root)
  out = File.join(root, 'stdout')
  err = File.join(root, 'stderr')
  spy = File.join(root, 'spy')
  argv = ['lookup'] + iso_args(root) +
         ['--hiera_config', './hiera.yaml', '--facts', './facts.yaml', '--node', NODE,
          '--render-as', job['render'] || 'json'] + job['argv']
  pid = fork do
    Dir.chdir(job['dir'])
    $stdout.reopen(out, 'w')
    $stderr.reopen(err, 'w')
    $stdout.sync = true
    $stderr.sync = true
    ENV['DIFF_SPY'] = spy
    ARGV.replace(argv)
    Puppet::Util::CommandLine.new('puppet', argv).execute
    exit!(0)
  end
  rc = nil
  begin
    Timeout.timeout(120) { _, st = Process.wait2(pid); rc = st.exitstatus }
  rescue Timeout::Error
    begin
      Process.kill('KILL', pid)
      Process.wait(pid)
    rescue SystemCallError
      nil
    end
    rc = -9
  end
  res = { 'id' => job['id'], 'rc' => rc, 'stdout' => read_text(out),
          'stderr' => read_text(err).gsub(/\e\[[0-9;]*m/, '') }
  res['lookup_error'] = JSON.parse(File.read(spy)) if File.exist?(spy)
  FileUtils.rm_rf(root)
  res
end

jobs = JSON.parse(File.read(ARGV[0]))
workers = (ARGV[2] || '8').to_i
iso_base = "/tmp/hyera-differential-#{Process.pid}"
FileUtils.mkdir_p(iso_base)
results = Array.new(jobs.size)
queue = Queue.new
jobs.each_with_index { |j, i| queue << [j, i] }
done = 0
mutex = Mutex.new
threads = Array.new(workers) do
  Thread.new do
    loop do
      j, i = begin
        queue.pop(true)
      rescue ThreadError
        break
      end
      r = run_job(j, iso_base, i)
      mutex.synchronize do
        results[i] = r
        done += 1
        warn "#{done}/#{jobs.size}" if (done % 50).zero? || done == jobs.size
      end
    end
  end
end
threads.each(&:join)
FileUtils.rm_rf(iso_base)
File.write(ARGV[1], JSON.generate(results))
