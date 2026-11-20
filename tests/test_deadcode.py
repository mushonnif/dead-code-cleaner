"""Run: python3 -m unittest discover -s tests"""
import io
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "remove-dead-code", "scripts"))
import deadcode  # noqa: E402


def write(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(body).lstrip("\n"))
    return p


def run(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = deadcode.main(list(argv))
    return code, buf.getvalue()


def ranges(path):
    found, why = deadcode.scan_file(path)
    assert why is None, why
    return [(f["start"], f["end"], f["level"]) for f in found]


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_php_commented_code_vs_explanation(self):
        p = write(self.d, "a.php", """
            <?php
            // Load the user model before querying.
            $this->load->model('user');
            // $this->db->where('id', $id);
            // $rows = $this->db->get('users')->result();
            /**
             * Docblock: @param int $id
             * $example = foo();
             */
            function a($id) {
                # $old = legacy($id);
                #[Attribute]
                return $id; // trailing note stays
            }
            /* if ($x) {
                   echo $x;
               } */
            """)
        self.assertEqual(ranges(p), [(4, 5, "high"), (11, 11, "high"), (15, 17, "high")])

    def test_python_strings_and_prose(self):
        p = write(self.d, "a.py", '''
            #!/usr/bin/env python3
            # -*- coding: utf-8 -*-
            """Docstring.
            # x = 1   <- inside a string, must not be touched
            """
            # Compute the total for every order in the list.
            # total = sum(o.price for o in orders)
            # print(total)
            def f():  # noqa
                # TODO: handle empty list
                # if not orders:
                #     return 0
                return 1
            ''')
        self.assertEqual(ranges(p), [(7, 8, "high"), (11, 12, "medium")])

    def test_js_template_literal_and_regex_like_globs(self):
        p = write(self.d, "a.js", """
            const glob = 'src/**/*.js';
            const tpl = `
            // not a comment, part of the string
            `;
            // const old = require('./old');
            // eslint-disable-next-line no-console
            console.log(glob, tpl);
            """)
        self.assertEqual(ranges(p), [(5, 5, "high")])

    def test_shell_heredoc(self):
        p = write(self.d, "a.sh", """
            cat <<EOF
            # echo $HOME inside heredoc
            EOF
            # rm -rf "$TMP_DIR"
            # echo "$x";
            echo ${#arr[@]}
            """)
        self.assertEqual(ranges(p), [(4, 5, "medium")])

    def test_html_and_sql(self):
        h = write(self.d, "a.html", """
            <!-- Main navigation -->
            <nav></nav>
            <!-- <div class="old">
              <span>legacy</span>
            </div> -->
            <!--[if IE]><p>ie</p><![endif]-->
            """)
        self.assertEqual(ranges(h), [(3, 5, "high")])
        s = write(self.d, "a.sql", """
            -- Active users only
            SELECT id FROM users WHERE active = 1;
            -- SELECT * FROM users WHERE deleted_at IS NULL;
            """)
        self.assertEqual([r[:2] for r in ranges(s)], [(3, 3)])

    def test_unterminated_block_skips_file(self):
        p = write(self.d, "a.c", "int x; /* never closed\n// int y = 2;\n")
        self.assertEqual(deadcode.scan_file(p), ([], "parse"))


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_level_high_and_collapse_blank_lines(self):
        p = write(self.d, "a.go", """
            package main

            // fmt.Println("debug")
            // x := compute(y)

            // Run starts the server.
            func Run() {}
            """)
        code, out = run("remove", "--level", "high", p)
        self.assertEqual(code, 0)
        with open(p) as fh:
            self.assertEqual(fh.read(), "package main\n\n// Run starts the server.\nfunc Run() {}\n")

    def test_remove_rejects_code_and_partial_blocks(self):
        p = write(self.d, "a.ts", """
            /* const a = 1;
               const b = 2; */
            const c = 3;
            // const d = 4;
            """)
        before = open(p).read()
        code, out = run("remove", p + ":1-1", p + ":3-4")
        self.assertIn("cuts block comment", out)
        self.assertIn("neither a comment", out)
        self.assertEqual(open(p).read(), before)
        code, out = run("remove", p + ":1-2", p + ":4")
        self.assertEqual(open(p).read(), "const c = 3;\n")

    def test_dry_run_keeps_file(self):
        p = write(self.d, "a.rb", "# puts user.name\nputs 1\n")
        run("remove", "--dry-run", p + ":1")
        self.assertEqual(open(p).read(), "# puts user.name\nputs 1\n")

    def test_crlf_preserved(self):
        p = os.path.join(self.d, "a.cs")
        with open(p, "wb") as fh:
            fh.write(b"// var x = Foo();\r\nint y = 1;\r\n")
        run("remove", p + ":1")
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), b"int y = 1;\r\n")


class UnusedTest(unittest.TestCase):
    def test_unreferenced_function(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "a.py", """
                def used():
                    return 1

                def _helper_never_called():
                    return used()
                """)
            write(d, "b.js", "export function shown() {}\nfunction lonelyThing() {}\nshown();\n")
            code, out = run("unused", d, "--root", d)
            self.assertIn("_helper_never_called [private]", out)
            self.assertIn("lonelyThing [public]", out)
            self.assertNotIn(" used ", out)
            self.assertNotIn("shown", out)


if __name__ == "__main__":
    unittest.main()


class DebugTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def found(self, path):
        found, why = deadcode.scan_file(path, kind="debug")
        assert why is None, why
        return [(f["start"], f["end"], f["level"] if f["level"] != "unsafe" else "!" + f["why"]) for f in found]

    def test_js(self):
        p = write(self.d, "a.js", """
            function f(x) {
                console.log('x', x);
                console.table(
                    rows
                );
                debugger;
                const s = console.log;
                if (x)
                    console.log(x);
                x && console.log(x);
                console.error('real error');
                alert('Satuan harus diisi');
                $.get(u).fail(function () {
                    console.log('failed');
                });
                const t = `
                console.log(inside template)
                `;
                return x;
            }
            """)
        self.assertEqual(self.found(p), [(2, 2, "high"), (3, 5, "high"), (6, 6, "high"),
                                         (9, 9, "!braceless-body"), (14, 14, "high")])

    def test_php(self):
        p = write(self.d, "a.php", """
            <?php
            var_dump($a);
            echo "<pre>"; print_r($post); die();
            $s = print_r($x, true);
            dd($request->all());
            echo json_encode($out); exit;
            $msg = "var_dump($x);";
            """)
        self.assertEqual(self.found(p), [(2, 2, "high"), (3, 3, "medium"), (5, 5, "high")])

    def test_debug_helper_functions_are_left_alone(self):
        p = write(self.d, "h.php", """
            <?php
            function dd($var)
            {
                var_dump($var);
                die;
            }
            function echopre($var) {
                echo '<pre>';
                print_r($var);
                exit;
            }
            function save($x) {
                var_dump($x);
                return $this->db->insert('t', $x);
            }
            ?>
            <script>
              console.log(data);
            </script>
            """)
        self.assertEqual(self.found(p), [(4, 4, "!debug-helper"), (9, 9, "!debug-helper"),
                                         (13, 13, "high"), (18, 18, "high")])
        py = write(self.d, "h.py", "def debug(x):\n    print(x)\n\ndef f(x):\n    print(x)\n    return x\n")
        self.assertEqual(self.found(py), [(2, 2, "!debug-helper"), (5, 5, "medium")])

    def test_python_only_statement_in_block(self):
        p = write(self.d, "a.py", """
            import pdb; pdb.set_trace()
            def f(x):
                print(x)
            if x:
                print(x)
            def g(x):
                print(x)
                return x
            breakpoint()
            """)
        self.assertEqual(self.found(p), [(1, 1, "high"), (3, 3, "!debug-helper"), (5, 5, "!only-statement"), (7, 7, "medium"), (9, 9, "high")])

    def test_ruby_go_rust(self):
        rb = write(self.d, "a.rb", "def f\n  binding.pry\n  p = 1\n  puts p\n  p\nend\nrequire 'json'\n")
        self.assertEqual(self.found(rb), [(2, 2, "high"), (4, 4, "medium")])
        go = write(self.d, "a.go", "package main\nfunc f() {\n\tfmt.Println(\"x\")\n\tspew.Dump(v)\n\trun()\n}\n")
        self.assertEqual(self.found(go), [(3, 3, "medium"), (4, 4, "high")])
        rs = write(self.d, "a.rs", "fn f() {\n    dbg!(x);\n    let y = dbg!(x);\n}\n")
        self.assertEqual(self.found(rs), [(2, 2, "high")])

    def test_remove_debug_and_reject_unsafe(self):
        p = write(self.d, "a.ts", """
            export function f(x: number) {
                console.log(x);
                if (x)
                    console.log(x);
                return x;
            }
            """)
        code, out = run("remove", "--level", "high", "--kind", "debug", p)
        self.assertIn("-1", out)
        code, out = run("remove", p + ":3")
        self.assertIn("unsafe context", out)
        with open(p) as fh:
            self.assertEqual(fh.read(), "export function f(x: number) {\n    if (x)\n"
                                        "        console.log(x);\n    return x;\n}\n")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def found(self, path):
        found, why = deadcode.scan_file(path, kind="imports")
        assert why is None, why
        return [(f["start"], f["end"], f["level"] if f["level"] != "unsafe" else "!" + f["why"]) for f in found]

    def test_python(self):
        p = write(self.d, "a.py", """
            from __future__ import annotations
            import os
            import sys, json
            from typing import List, Dict
            from collections import (
                Counter,
                OrderedDict,
            )
            from x import *
            import readline  # noqa: F401
            from .models import User
            # from .models import Admin
            try:
                import ujson as fastjson
            except ImportError:
                fastjson = None
            __all__ = ["User"]
            print(os.sep, sys.argv)
            # c = Counter()
            def f(x: List[int]):
                return OrderedDict()
            """)
        self.assertEqual(self.found(p), [(3, 3, "!partial"), (4, 4, "!partial"), (5, 8, "!partial")])
        q = write(self.d, "b.py", "import os\nfrom a import b\ntry:\n    import ujson\nexcept ImportError:\n    pass\n")
        self.assertEqual(self.found(q), [(1, 1, "medium"), (2, 2, "high"), (4, 4, "!only-statement")])
        init = write(self.d, "__init__.py", "from .a import b\n")
        self.assertEqual(self.found(init), [])

    def test_js_ts(self):
        p = write(self.d, "a.tsx", """
            import React from 'react';
            import './styles.css';
            import def, { a, b as c, type T } from './x';
            import * as ns from 'ns';
            import {
              unusedOne,
              usedOne,
            } from './y';
            import type { Props } from './types';
            import lonely from 'lonely';
            // eslint-disable-next-line
            import kept from 'kept';
            const fs = require('fs');
            const { join, resolve: res } = require('path');
            export function C(p: Props) {
              const t: T = a;
              return <div>{usedOne(c, join('x'))}</div>;
            }
            """)
        self.assertEqual(self.found(p), [(1, 1, "medium"), (3, 3, "!partial"), (4, 4, "high"),
                                         (5, 8, "!partial"), (10, 10, "high"), (13, 13, "medium"),
                                         (14, 14, "!partial")])

    def test_vue_template_usage(self):
        p = write(self.d, "a.vue", """
            <template>
              <my-button />
              <p>{{ $count }}</p>
            </template>
            <script>
            import MyButton from './MyButton.vue';
            import Other from './Other.vue';
            import { count } from './store';
            </script>
            """)
        self.assertEqual(self.found(p), [(7, 7, "medium")])

    def test_php_use_and_traits(self):
        p = write(self.d, "a.php", """
            <?php
            namespace App;
            use App\\Models\\User;
            use App\\Models\\Post as Article;
            use Illuminate\\Support\\{Str, Arr};
            use function App\\helpers\\fmt;
            use App\\Contracts\\Logger;
            class A {
                use SomeTrait;
                /** @var Logger */
                private $log;
                public function f() {
                    return function () use ($x) { return user::find(1) . Str::x(); };
                }
            }
            """)
        self.assertEqual(self.found(p), [(4, 4, "high"), (5, 5, "!partial"), (6, 6, "high")])

    def test_java_kotlin(self):
        j = write(self.d, "A.java", "import java.util.List;\nimport java.util.Map;\nimport java.io.*;\n"
                                    "class A { List<String> x; }\n")
        self.assertEqual(self.found(j), [(2, 2, "high")])
        k = write(self.d, "a.kt", "import a.b.Foo\nimport a.b.Bar as Baz\nimport x.getValue\nval y = Foo()\n")
        self.assertEqual(self.found(k), [(2, 2, "medium")])

    def test_remove_imports(self):
        p = write(self.d, "a.ts", """
            import { a } from './a';
            import { b, c } from './b';
            import d from './d';

            export const x = b;
            """)
        code, out = run("remove", "--level", "high", "--kind", "imports", p)
        self.assertIn("-2", out)
        self.assertEqual(open(p).read(), "import { b, c } from './b';\n\nexport const x = b;\n")
        code, out = run("remove", p + ":1")
        self.assertIn("unsafe context (partial)", out)
