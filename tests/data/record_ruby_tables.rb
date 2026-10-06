# Dev-only recorder (never collected by pytest): runs Ruby's Dir.glob and
# Pathname#+ over fixed inputs and writes the two tables the tests replay.
#
#   ruby tests/data/record_ruby_tables.rb        # from the repository root
#
# Writes tests/data/dir_glob_table.json and tests/data/pathname_plus_table.json.
# Recorded with Ruby 4.0 on Linux; review the diff before committing a re-run.
require "json"
require "pathname"
require "tmpdir"

HERE = File.expand_path(__dir__)

TREE = %w(
  a.yaml b.yaml ab.yaml abc.yaml aXc.yaml Case.yaml 1.yaml _u.yaml z.yaml
  .hidden.yaml data[1].yaml data1.yaml a-b.yaml a]b.yaml x{y}.yaml a,b.yaml
  !.yaml ^.yaml plain a.txt é.yaml 日本.yaml
  .hd/e.yaml .hd/.f.yaml .hd/sub/g.yaml
  sub/c.yaml sub/d.txt sub/.g.yaml sub/deep/e.yaml sub/deep/deeper/f.yaml
  sub/deep/.dh/i.yaml
  other/c.yaml other/a.yaml other/x/y/c.yaml
  emptydir/
) + ["sp ace.yaml"]

STARS = [
  "*", "*.yaml", "a*", "*a", "*a*", "a*.yaml", "*b*.yaml", "a**", "**a", "a*c*",
  "**", "***", "*.*", "*.*.*", "a*b*c*", "*****.yaml", "*a*a*a*a*a*a*b.yaml",
  "?", "??", "???", "?.yaml", "??.yaml", "a?.yaml", "?b.yaml", "a?c.yaml", "?*",
  "*?", "*?*", "a*?.yaml", "????????????.yaml", "*.y*", "*.y?ml", "*.yaml*",
  "*yaml", "*l", "*.", "a.*", "*.txt", "*.TXT",
]

BRACKETS = [
  "[ab].yaml", "[a-b].yaml", "[a-c]*.yaml", "[!a].yaml", "[^a].yaml", "[!a-b].yaml",
  "[^a-b]*", "[z-a].yaml", "[a-\\z].yaml", "[b-a].yaml", "[a-a].yaml", "[]a].yaml",
  "[!]a].yaml", "[^]].yaml", "[a-].yaml", "[-a].yaml", "[a-b-c].yaml", "[a", "a[",
  "[a.yaml", "[a-b", "[a-b.yaml", "[a-", "[", "[]", "[!", "[!]", "[^]", "[\\]].yaml",
  "a\\]b.yaml", "a[\\]]b.yaml", "a[]]b.yaml", "data[1].yaml", "data[[]1[]].yaml",
  "data\\[1\\].yaml", "data[1-9].yaml", "[[:alpha:]].yaml", "[[:digit:]].yaml",
  "[A-Z]*.yaml", "[a-zA-Z]*.yaml", "[0-9].yaml", "[0-9]*", "[_-a].yaml", "[\\a-c].yaml",
  "[a-\\c].yaml", "[\\-].yaml", "[a\\-c].yaml", "[é].yaml", "[日本]*", "[a-é].yaml",
  "?[!a]?.yaml", "[!.]*.yaml", "[.]hidden.yaml", "[.]*", "[^.]*", "[!^a].yaml", "[^!a].yaml",
  "[ab-].yaml", "[*].yaml", "[?].yaml", "[{].yaml", "[a]b].yaml", "[\\z-a].yaml",
]

ESCAPES = [
  "\\*.yaml", "a\\.yaml", "\\a.yaml", "a.yaml\\", "a.yaml\\\\", "\\\\", "\\", "a\\*",
  "*\\", "\\.hidden.yaml", "\\.*", "\\?.yaml", "\\[a].yaml", "a\\b\\c.yaml", "\\*",
  "\\{a\\}.yaml", "x\\{y\\}.yaml", "sub\\/c.yaml", "su\\b/c.yaml", "sub/\\c.yaml",
  "\\é.yaml", "a.y\\aml", "a.yaml\\*", "*.yaml\\", "?\\", "[a]\\",
]

DOTS = [
  ".*", ".*.yaml", ".h*", ".?*", ".**", ".*/a.yaml", ".*/*.yaml", ".*/*", ".*/.*",
  ".hd/*", ".hd/.*", ".hd/**", ".hd/**/*.yaml", ".hd/**/.*", ".hd/**/.*.yaml",
  ".hidden.yaml", ".hd/e.yaml", "*/.*", "*/.g.yaml", "sub/.*", "sub/.g.yaml",
  "**/.*", "**/.g.yaml", "**/.*.yaml", "**/.hd/e.yaml", "**/.dh/i.yaml", "**/*.yaml",
  "**/.*/*.yaml", "**/.*/**/*.yaml", ".*/**/*.yaml", ".*/**", "./a.yaml", "./*.yaml",
  "./.*", "./.hd/e.yaml", "./**/*.yaml", "sub/./c.yaml", "sub/./*.yaml", "sub/../a.yaml",
  "sub/../*.yaml", "../a.yaml", "..", ".", "./", "./.", "./..", "*/..", "*/.", "sub/.",
  "sub/..", "sub/../sub/c.yaml", ".*/.", ".*/..", ".?", ".??", "..*", "...", ".[.]*",
  ".[a-z]*", ".*e*", ".*/e.yaml", ".*/*/g.yaml", ".*/**/g.yaml",
]

RECURSIVE = [
  "**/*.yaml", "**/c.yaml", "**/*", "**/", "**/**/c.yaml", "**/**/*.yaml", "**/sub/**/*.yaml",
  "sub/**/*.yaml", "sub/**", "sub/**/", "sub/**/*", "sub/**/e.yaml", "sub/**/deeper/*.yaml",
  "**/deep/e.yaml", "**/deeper/f.yaml", "**/x/**/c.yaml", "**/x/y/c.yaml", "other/**/c.yaml",
  "**/nonexistent", "**/nonexistent/*.yaml", "*/**/c.yaml", "*/**/*.yaml", "**/*/c.yaml",
  "**/other/*.yaml", "**/*e*.yaml", "**/e*", "**/[ce].yaml", "**/?.yaml", "**/??.yaml",
  "***/c.yaml", "**a/c.yaml", "a**/c.yaml", "**/a**", "**/**", "**/**/**", "**/*/**/*.yaml",
  "sub/**/**/e.yaml", "sub/**/deep/**/f.yaml", "**/emptydir", "**/emptydir/*", "emptydir/**",
  "emptydir/**/*", "**/sub", "**/deep", "**/deep/**", "**/*.TXT", "**/d.txt", "**/D.txt",
  "**/Sub/c.yaml", "**/C.yaml", "**/c.y*", "**/{c,e}.yaml", "{sub,other}/**/c.yaml",
  "**/.dh", "**/i.yaml", "**/sub/c.yaml", "**/plain",
]

BRACES = [
  "{a,b}.yaml", "{a,b,}.yaml", "{,a}.yaml", "{a}.yaml", "{}.yaml", "{,}.yaml", "{a,{b,c}}.yaml",
  "{a,b{c,d}}.yaml", "{{a,b},c}.yaml", "a{b,c}*.yaml", "{sub,other}/c.yaml",
  "{sub,other}/**/*.yaml", "{a.yaml,sub/c.yaml}", "\\{a,b\\}.yaml", "{a,b", "a}", "}a", "{",
  "}", "a{", "*{a,b}.yaml", "{*,.*}", "{a,b}{c,d}", "{a,b}{.yaml,.txt}", "{a,a}.yaml",
  "{b,a}.yaml", "a{,b}.yaml", "{a,b\\,c}.yaml", "{a\\,b,c}.yaml", "x{y}.yaml", "x\\{y\\}.yaml",
  "{x\\{y\\}}.yaml", "{x{y}}.yaml", "x{{y}}.yaml", "{a{b,c}.yaml", "a{b,c}}.yaml",
  "{sub/c,other/c}.yaml", "{sub,other}/{c,a}.yaml", "{sub/**,other/**}/*.yaml",
  "sub/{c,d}.*", "{*}.yaml", "{*.yaml,*.txt}", "{?,??}.yaml", "{[ab],c}.yaml", "{a,[b}.yaml",
  "{.hd,sub}/*", "{.*,*}.yaml", "{a,b}/", "{a/b,c}", "{,sub/}c.yaml", "{,sub/}*.yaml",
  "{.,sub}/c.yaml", "{..,sub}/c.yaml", "a,b.yaml", "{a\\}.yaml", "{a\\},b}.yaml", "{\\{,\\}}",
  "{a,{b,{c,{d,e}}}}.yaml", "{{{{a}}}}.yaml", "{{{{a,b}}}}.yaml", "{é,日本}.yaml",
  "{sp\\ ace,a}.yaml", "{sp ace,a}.yaml", 
]

MISC = [
  "a.yaml", "A.yaml", "nonexistent.yaml", "nonexistent/*.yaml", "*.nothing", "sub", "sub/",
  "sub/*", "sub/*/", "sub/c.yaml", "sub/deep/e.yaml", "Sub/c.yaml", "Sub/*.yaml",
  "*/C.yaml", "*/c.yaml", "*/*.yaml", "*/*/*.yaml", "*/*/*/*.yaml", "*/*/*/*/*.yaml",
  "*/*", "*/*/*", "sub/*/*.yaml", "sub/deep/*", "sub/deep/*/*", "*/deep", "*/deep/*",
  "s*/d*/e.yaml", "s?b/c.yaml", "su[a-c]/c.yaml", "su[b]/c.yaml", "other/x/y/c.yaml",
  "other/*/y/*.yaml", "other/*/*/c.yaml", "o*/*/*/c.yaml", "emptydir", "emptydir/*",
  "emptydir/", "plain", "pla*", "p?ain", "*.", "é.yaml", "?.yaml", "日本.yaml", "??.yaml",
  "日*", "sp ace.yaml", "sp*ace.yaml", "sp?ce.yaml", "sp[ ]ace.yaml", "x{y}.yaml",
  "a,b.yaml", "!.yaml", "^.yaml", "[!].yaml", "[^].yaml", "a-b.yaml", "a]b.yaml",
  "data1.yaml", "data[1].yaml", "d*", "d*[1].yaml", "d*\\[1\\].yaml", "d*[[]1].yaml",
  "*/", "*//", "/", "", "*.yaml/", "a.yaml/", "a.yaml//", "sub/c.yaml/",
]

# Not recorded: a pattern ending in "/" (directories only, which Puppet rejects),
# a brace group directly after "**" (Ruby matches it per directory entry, in sorted
# order), a brace group that expands to an empty pattern, and a doubled "/"
# (Ruby keeps it in the result).
PATTERNS = (STARS + BRACKETS + ESCAPES + DOTS + RECURSIVE + BRACES + MISC)
  .uniq.reject { |p| p.empty? || p.end_with?("/") }

def build_tree(root)
  TREE.each do |rel|
    path = File.join(root, rel)
    if rel.end_with?("/")
      FileUtils.mkdir_p(path)
    else
      FileUtils.mkdir_p(File.dirname(path))
      File.write(path, "")
    end
  end
end

require "fileutils"
Dir.mktmpdir("hyera-glob-", Dir.home) do |root|
  build_tree(root)
  rows = PATTERNS.map do |pat|
    [pat, Dir.glob(pat, base: root)]
  end
  File.write(File.join(HERE, "dir_glob_table.json"), JSON.pretty_generate(
    "ruby" => RUBY_VERSION, "tree" => TREE, "rows" => rows
  ) + "\n")
  puts "dir_glob_table: #{rows.size} rows"
end

BASES = [
  "/r/data", "/r/data/", "/r", "/", "/r/data/sub/..", "/r/data/..", "/r/./data", "r/data",
  "r", ".", "..", "../x", "", "a/..", "/r//data", "/r/data//", "//r/data", "c:/r",
  "/r/data/sub/../..", "/a/b/c/d", "../..", "./a", "a/b/../..", "/r/data/..//sub",
]

RELS = [
  "a.yaml", "./a.yaml", "../a.yaml", "../../a.yaml", "../../../../a.yaml", "..", ".", "",
  "a.yaml/", "sub/../c.yaml", "/abs/a.yaml", "//unc/share/a.yaml", "c:/a.yaml",
  ".//a.yaml", "..//a.yaml", "a/./b", ".../a", "...", "../..", "./..", "a/..", "x/../../y",
  "../", "../a/", "./", "././a", "../.hidden", "sub/", "a b/c d.yaml", "..//..//a",
]

pairs = []
BASES.each do |b|
  RELS.each do |r|
    pairs << [b, r, (Pathname.new(b) + r).to_s]
  end
end
File.write(File.join(HERE, "pathname_plus_table.json"), JSON.pretty_generate(
  "ruby" => RUBY_VERSION, "rows" => pairs
) + "\n")
puts "pathname_plus_table: #{pairs.size} rows"
