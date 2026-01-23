import pytest
import os
from parse_coverage import (
    parse_diff,
    parse_coverage_and_generate_report,
    merge_coverage_files,
    diff_coverage,
    normalize_path,
)


class TestParseDiff:
    """测试 diff 解析"""

    def test_normal_hunk(self, tmp_path):
        """正常的 @@ -x,y +x,y @@ 格式
        
        @@ -10,6 +10,8 @@ 表示新文件从第10行开始
        - 行10: context (existing code)
        - 行11: + newVar := 1
        - 行12: + fmt.Println(newVar)
        - 行13: context (more existing)
        """
        diff_content = """\
diff --git a/pkg/sql/parser.go b/pkg/sql/parser.go
--- a/pkg/sql/parser.go
+++ b/pkg/sql/parser.go
@@ -10,6 +10,8 @@ func Parse() {
    existing code
+    newVar := 1
+    fmt.Println(newVar)
    more existing
}
"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(diff_content)
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        result = parse_diff(str(diff_file), str(ignore_file))

        assert "pkg/sql/parser.go" in result
        lines = [item[0] for item in result["pkg/sql/parser.go"]]
        assert 11 in lines  # 第一个新增行
        assert 12 in lines  # 第二个新增行

    @pytest.mark.xfail(reason="BUG: 正则无法匹配 @@ -0,0 +1 @@ 格式（缺少逗号）")
    def test_single_line_hunk_no_comma(self, tmp_path):
        """问题场景: @@ -0,0 +1 @@ 格式（新文件单行）"""
        diff_content = """\
diff --git a/pkg/newpkg/new.go b/pkg/newpkg/new.go
new file mode 100644
--- /dev/null
+++ b/pkg/newpkg/new.go
@@ -0,0 +1 @@
+package newpkg
"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(diff_content)
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        result = parse_diff(str(diff_file), str(ignore_file))

        assert "pkg/newpkg/new.go" in result, "单行 hunk 格式未被正确解析"
        assert len(result["pkg/newpkg/new.go"]) == 1

    @pytest.mark.xfail(reason="BUG: 正则无法匹配 @@ -5 +5 @@ 格式（两边都无逗号）")
    def test_single_line_change(self, tmp_path):
        """问题场景: @@ -5 +5 @@ 格式（单行修改）"""
        diff_content = """\
diff --git a/pkg/util/helper.go b/pkg/util/helper.go
--- a/pkg/util/helper.go
+++ b/pkg/util/helper.go
@@ -5 +5 @@ func Helper() {
-    old := 1
+    new := 2
"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(diff_content)
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        result = parse_diff(str(diff_file), str(ignore_file))

        assert "pkg/util/helper.go" in result, "单行修改格式未被正确解析"

    def test_new_file_multiple_lines(self, tmp_path):
        """新文件多行 @@ -0,0 +1,5 @@ 格式"""
        diff_content = """\
diff --git a/pkg/new/file.go b/pkg/new/file.go
new file mode 100644
--- /dev/null
+++ b/pkg/new/file.go
@@ -0,0 +1,5 @@
+package new
+
+func Foo() {
+    x := 1
+}
"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text(diff_content)
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        result = parse_diff(str(diff_file), str(ignore_file))

        assert "pkg/new/file.go" in result
        lines = [item[0] for item in result["pkg/new/file.go"]]
        assert 1 in lines  # package new
        assert 4 in lines  # x := 1


class TestCoverageMatching:
    """测试覆盖率匹配逻辑"""

    def test_line_inside_block(self, tmp_path):
        """修改行在 coverage 块内部 (start < line < end)"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,15.2 3 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [(12, 5, 16)]  # 行12在10-15之间
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 1
        assert covered == 1
        assert pct == 1.0

    def test_line_on_boundary_with_overlap(self, tmp_path):
        """修改行在边界，列有重叠"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:12.10,12.30 1 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [(12, 5, 20)]  # 列5-20 与 10-30 有重叠
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 1
        assert covered == 1

    def test_line_on_boundary_no_overlap(self, tmp_path):
        """修改行在边界，列无重叠"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:12.20,12.30 1 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [(12, 5, 15)]  # 列5-15 与 20-30 无重叠
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 0  # 无匹配
        assert covered == 0

    def test_multiple_modified_lines_one_block_current_behavior(self, tmp_path):
        """按块计数(默认): 3行在同一个块内 → total=1"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,20.2 5 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (12, 1, 10),
                (14, 1, 10),
                (16, 1, 10),
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        # 默认按块计数
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 1
        assert covered == 1
        assert pct == 1.0

    def test_multiple_modified_lines_one_block_by_line(self, tmp_path):
        """按行计数: 3行在同一个块内 → total=3"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,20.2 5 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (12, 1, 10),
                (14, 1, 10),
                (16, 1, 10),
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 3
        assert covered == 3
        assert pct == 1.0

    def test_multiple_lines_partial_coverage(self, tmp_path):
        """按块计数(默认): 2块，1covered → 50%"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,13.2 2 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:14.1,16.2 2 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (11, 1, 10),  # covered
                (12, 1, 10),  # covered
                (15, 1, 10),  # NOT covered
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 2
        assert covered == 1
        assert pct == 0.5

    def test_multiple_lines_partial_coverage_by_line(self, tmp_path):
        """按行计数: 3行，2covered → 66.7%"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,13.2 2 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:14.1,16.2 2 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (11, 1, 10),  # covered
                (12, 1, 10),  # covered
                (15, 1, 10),  # NOT covered
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 3
        assert covered == 2
        assert abs(pct - 2/3) < 0.01

    def test_uncovered_block(self, tmp_path):
        """未覆盖的代码块"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,15.2 3 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [(12, 5, 16)]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        assert total == 1
        assert covered == 0
        assert pct == 0.0


class TestPathNormalization:
    """测试路径规范化"""

    def test_with_prefix(self):
        path = "github.com/matrixorigin/matrixone/pkg/sql/parser.go"
        result = normalize_path(path)
        assert result == "pkg/sql/parser.go"

    def test_without_prefix(self):
        path = "pkg/sql/parser.go"
        result = normalize_path(path, "")
        assert result == "pkg/sql/parser.go"

    @pytest.mark.xfail(reason="BUG: normalize_path 在 prefix 为空时不调用 os.path.normpath")
    def test_relative_path(self):
        """./path 应该被规范化为 path"""
        path = "./pkg/sql/parser.go"
        result = normalize_path(path, "")
        assert result == "pkg/sql/parser.go"

    def test_path_mismatch_scenario(self, tmp_path):
        """路径不匹配导致覆盖率为0的场景"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,15.2 3 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        # 如果 modified_lines 的 key 带完整前缀，会匹配失败
        modified_lines = {
            "github.com/matrixorigin/matrixone/pkg/sql/parser.go": [(12, 5, 16)]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        # coverage 中的路径被 normalize 去掉前缀
        # 但 modified_lines 的 key 保持原样，导致不匹配
        assert total == 0, "路径不匹配导致无法匹配"


class TestMergeCoverage:
    """测试覆盖率合并"""

    def test_merge_two_files(self, tmp_path):
        """合并两个 coverage 文件，同一块只要任一覆盖就算覆盖"""
        cov1 = tmp_path / "ut.out"
        cov1.write_text("""\
mode: set
github.com/matrixorigin/matrixone/pkg/a.go:1.1,5.2 3 1
github.com/matrixorigin/matrixone/pkg/b.go:1.1,5.2 3 0
""")
        cov2 = tmp_path / "bvt.out"
        cov2.write_text("""\
mode: set
github.com/matrixorigin/matrixone/pkg/a.go:1.1,5.2 3 0
github.com/matrixorigin/matrixone/pkg/b.go:1.1,5.2 3 1
""")
        output = tmp_path / "merged.out"

        total, covered, pct = merge_coverage_files(str(output), str(cov1), str(cov2))

        assert total == 2
        assert covered == 2  # 两个块都至少被一个测试覆盖
        assert pct == 1.0


class TestEndToEnd:
    """端到端测试"""

    def test_full_flow_by_block(self, tmp_path):
        """完整流程测试 - 默认按块计数"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text("""\
diff --git a/pkg/sql/parser.go b/pkg/sql/parser.go
--- a/pkg/sql/parser.go
+++ b/pkg/sql/parser.go
@@ -10,4 +10,7 @@ func Parse() {
    existing
+    line1 := 1
+    line2 := 2
+    line3 := 3
    more
}
""")
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        cov_file = tmp_path / "coverage.out"
        cov_file.write_text("""\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,13.2 2 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:14.1,16.2 2 0
""")

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = diff_coverage(
            str(diff_file), str(cov_file), str(output_file), str(ignore_file)
        )

        # 默认按块计数
        assert total >= 1
        assert covered >= 1

    def test_full_flow_by_line(self, tmp_path):
        """完整流程测试 - 按行计数"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text("""\
diff --git a/pkg/sql/parser.go b/pkg/sql/parser.go
--- a/pkg/sql/parser.go
+++ b/pkg/sql/parser.go
@@ -10,4 +10,7 @@ func Parse() {
    existing
+    line1 := 1
+    line2 := 2
+    line3 := 3
    more
}
""")
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")

        cov_file = tmp_path / "coverage.out"
        cov_file.write_text("""\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,13.2 2 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:14.1,16.2 2 0
""")

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = diff_coverage(
            str(diff_file), str(cov_file), str(output_file), str(ignore_file),
            count_by_line=True
        )

        # 按行计数
        assert total >= 2
        assert covered >= 2

    def test_no_modified_go_files(self, tmp_path):
        """没有修改 go 文件时返回 100%"""
        diff_file = tmp_path / "diff.patch"
        diff_file.write_text("""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Title
+New line
 Content
""")
        ignore_file = tmp_path / ".ignore"
        ignore_file.write_text("!*.go\n")  # 只包含 .go 文件

        cov_file = tmp_path / "coverage.out"
        cov_file.write_text("mode: set\n")

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = diff_coverage(
            str(diff_file), str(cov_file), str(output_file), str(ignore_file)
        )

        assert total == 0
        assert covered == 0
        assert pct == 1  # 无修改时返回 100%


# ============================================================
# 按块 vs 按行计数对比测试 - 改进后应通过
# ============================================================

class TestBlockVsLineCoverage:
    """对比按块计数和按行计数的差异 - 改进后按行计数"""

    def test_case1_many_lines_in_covered_block(self, tmp_path):
        """场景1: 按行计数 - 10行covered + 1行uncovered → 90.9%"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,30.2 10 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:31.1,35.2 3 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (11, 1, 10), (12, 1, 10), (13, 1, 10), (14, 1, 10), (15, 1, 10),
                (16, 1, 10), (17, 1, 10), (18, 1, 10), (19, 1, 10), (20, 1, 10),
                (32, 1, 10),
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 11
        assert covered == 10
        assert abs(pct - 10/11) < 0.01

    def test_case2_one_line_in_large_uncovered_block(self, tmp_path):
        """场景2: 按行计数 - 1行covered + 10行uncovered → 9.1%"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,15.2 3 1
github.com/matrixorigin/matrixone/pkg/sql/parser.go:20.1,40.2 10 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (12, 1, 10),
                (21, 1, 10), (22, 1, 10), (23, 1, 10), (24, 1, 10), (25, 1, 10),
                (26, 1, 10), (27, 1, 10), (28, 1, 10), (29, 1, 10), (30, 1, 10),
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 11
        assert covered == 1
        assert abs(pct - 1/11) < 0.01

    def test_case3_realistic_pr_scenario(self, tmp_path):
        """场景3: 按行计数 - 重构大函数 18/20 → 90%"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/executor.go:100.1,150.2 25 1
github.com/matrixorigin/matrixone/pkg/sql/executor.go:151.1,155.2 3 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {"pkg/sql/executor.go": []}
        for i in range(110, 128):
            modified_lines["pkg/sql/executor.go"].append((i, 1, 20))
        modified_lines["pkg/sql/executor.go"].append((152, 1, 20))
        modified_lines["pkg/sql/executor.go"].append((153, 1, 20))

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 20
        assert covered == 18
        assert abs(pct - 0.9) < 0.01

    def test_case4_multiple_small_blocks(self, tmp_path):
        """场景4: 每块1行时，按块和按行结果相同"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/a.go:10.1,12.2 1 1
github.com/matrixorigin/matrixone/pkg/a.go:15.1,17.2 1 1
github.com/matrixorigin/matrixone/pkg/a.go:20.1,22.2 1 1
github.com/matrixorigin/matrixone/pkg/a.go:25.1,27.2 1 0
github.com/matrixorigin/matrixone/pkg/a.go:30.1,32.2 1 0
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/a.go": [
                (11, 1, 10), (16, 1, 10), (21, 1, 10),  # covered
                (26, 1, 10), (31, 1, 10),  # NOT covered
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file)
        )

        # 每块1行时结果相同，此测试应该通过
        assert total == 5
        assert covered == 3
        assert pct == 0.6

    def test_case5_same_block_multiple_lines(self, tmp_path):
        """场景5: 按行计数 - 3行在同一个covered块 → total=3"""
        coverage_content = """\
mode: set
github.com/matrixorigin/matrixone/pkg/sql/parser.go:10.1,20.2 5 1
"""
        cov_file = tmp_path / "coverage.out"
        cov_file.write_text(coverage_content)

        modified_lines = {
            "pkg/sql/parser.go": [
                (12, 1, 10),
                (14, 1, 10),
                (16, 1, 10),
            ]
        }

        output_file = tmp_path / "pr_coverage.out"
        total, covered, pct = parse_coverage_and_generate_report(
            str(cov_file), modified_lines, str(output_file), count_by_line=True
        )

        assert total == 3
        assert covered == 3
        assert pct == 1.0
