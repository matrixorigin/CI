package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const coverageHeader = "mode: set\n"

func TestCoveragePackagesKeepsReportScopeAndRejectsIncompleteInput(t *testing.T) {
	input := "example.com/pkg/work\nexample.com/pkg/pb/model\nexample.com/pkg/sql/parsers/goyacc\nexample.com/pkg/untested\n"
	var output bytes.Buffer
	if err := coveragePackages(strings.NewReader(input), &output); err != nil {
		t.Fatal(err)
	}
	if got, want := output.String(), "example.com/pkg/work,example.com/pkg/untested\n"; got != want {
		t.Fatalf("instrumentation scope=%q, want %q", got, want)
	}
	for _, input := range []string{"", "example.com/pkg/pb/model\n", "example.com/a,example.com/b\n", "example.com/a\n\n"} {
		output.Reset()
		if err := coveragePackages(strings.NewReader(input), &output); err == nil || output.Len() != 0 {
			t.Fatalf("invalid scope %q: error=%v output=%q", input, err, output.String())
		}
	}
	if err := coveragePackages(failureReader{}, &output); err == nil {
		t.Fatal("lost scope read failure")
	}
	if err := coveragePackages(strings.NewReader("example.com/a\n"), failureWriter{}); err == nil {
		t.Fatal("lost scope write failure")
	}
}

func TestCoveragePackagesPreservesCrossPackageHitsAndUntestedBlocks(t *testing.T) {
	dir := t.TempDir()
	files := map[string]string{
		"go.mod":                     "module example.com\n\ngo 1.23.0\n",
		"pkg/pb/model/model.go":      "package model\nfunc Normalize(n int) int { if n < 0 { return -n }; return n }\n",
		"pkg/work/work.go":           "package work\nimport \"example.com/pkg/pb/model\"\nfunc Process(n int) int { if n == 0 { return 0 }; return model.Normalize(n)+1 }\n",
		"pkg/work/work_test.go":      "package work\nimport \"testing\"\nfunc TestZero(t *testing.T) { if Process(0) != 0 { t.Fatal(\"zero\") } }\n",
		"pkg/pb/model/model_test.go": "package model_test\nimport (\"testing\"; \"example.com/pkg/work\")\nfunc TestDependencyFromExcludedPackage(t *testing.T) { if work.Process(-2) != 3 { t.Fatal(\"dependency\") } }\n",
		"pkg/pb/plain/plain.go":      "package plain\nfunc Value() int { return 7 }\n",
		"pkg/pb/plain/plain_test.go": "package plain\nimport \"testing\"\nfunc TestExcludedOnly(t *testing.T) { if Value() != 7 { t.Fatal(\"value\") } }\n",
		"pkg/untested/u.go":          "package untested\nfunc Uncalled() int { return 42 }\n",
	}
	for name, data := range files {
		path := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	var scope bytes.Buffer
	if err := coveragePackages(strings.NewReader("example.com/pkg/pb/model\nexample.com/pkg/pb/plain\nexample.com/pkg/untested\nexample.com/pkg/work\n"), &scope); err != nil {
		t.Fatal(err)
	}
	var profiles []string
	for _, coverpkg := range []string{"./...", strings.TrimSpace(scope.String())} {
		profile := filepath.Join(dir, "coverage.out")
		cmd := exec.CommandContext(ctx, "go", "test", "-count=1", "-v", "-covermode=set", "-coverpkg="+coverpkg, "-coverprofile="+profile, "./...")
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GOWORK=off", "GOFLAGS=")
		cmd.WaitDelay = time.Second
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("go test: %v\n%s", err, output)
		}
		if !bytes.Contains(output, []byte("--- PASS: TestDependencyFromExcludedPackage")) {
			t.Fatalf("excluded-package test did not execute:\n%s", output)
		}
		if !bytes.Contains(output, []byte("--- PASS: TestExcludedOnly")) {
			t.Fatalf("excluded-only package test did not execute:\n%s", output)
		}
		raw, err := os.ReadFile(profile)
		if err != nil {
			t.Fatal(err)
		}
		var result bytes.Buffer
		if _, err := compact(bytes.NewReader(raw), &result); err != nil {
			t.Fatal(err)
		}
		profiles = append(profiles, result.String())
	}
	if profiles[0] != profiles[1] {
		t.Fatalf("published blocks/hits changed:\nbaseline:\n%scandidate:\n%s", profiles[0], profiles[1])
	}
	retainedZero := false
	for _, line := range strings.Split(profiles[1], "\n") {
		if strings.HasPrefix(line, "example.com/pkg/untested/u.go:") && strings.HasSuffix(line, " 1 0") {
			retainedZero = true
		}
	}
	if !retainedZero {
		t.Fatalf("lost untested-package denominator:\n%s", profiles[1])
	}
}

func TestCompactUnionsExactBlocksAndExclusions(t *testing.T) {
	input := coverageHeader +
		"example.com/a.go:1.1,1.2 1 0\n" +
		"example.com/b.go:2.1,2.2 2 1\n" +
		"example.com/a.go:1.1,1.2 1 1\n" +
		"example.com/a.go:1.1,1.2 2 0\n" +
		"example.com/b.go:2.1,2.2 2 0\n" +
		"example.com/pkg/pb/p.go:1.1,1.2 1 1\n" +
		"example.com/pkg/sql/parsers/goyacc/g.go:1.1,1.2 1 1\n" +
		"example.com/yaccparser/y.go:1.1,1.2 1 1\n"
	var output bytes.Buffer
	blocks, err := compact(strings.NewReader(input), &output)
	if err != nil {
		t.Fatal(err)
	}
	if blocks != 3 {
		t.Fatalf("blocks=%d, want 3", blocks)
	}
	want := coverageHeader +
		"example.com/a.go:1.1,1.2 1 1\n" +
		"example.com/a.go:1.1,1.2 2 0\n" +
		"example.com/b.go:2.1,2.2 2 1\n"
	if output.String() != want {
		t.Fatalf("output:\n%s\nwant:\n%s", output.String(), want)
	}
}

func TestCompactCrossesReaderBufferWithoutLosingRecords(t *testing.T) {
	var input strings.Builder
	input.WriteString(coverageHeader)
	for i := 0; i < 40000; i++ {
		input.WriteString("example.com/a.go:1.1,1.2 1 0\n")
	}
	input.WriteString("example.com/a.go:1.1,1.2 1 1\n")
	var output bytes.Buffer
	blocks, err := compact(strings.NewReader(input.String()), &output)
	if err != nil || blocks != 1 {
		t.Fatalf("blocks=%d error=%v", blocks, err)
	}
	if output.String() != coverageHeader+"example.com/a.go:1.1,1.2 1 1\n" {
		t.Fatalf("last duplicate was lost: %q", output.String())
	}
}

func TestCompactRejectsMalformedInputs(t *testing.T) {
	cases := []string{
		"mode: count\nexample.com/a.go:1.1,1.2 1 1\n",
		coverageHeader,
		coverageHeader + "example.com/pkg/pb/a.go:1.1,1.2 1 1\n",
		coverageHeader + "example.com/a.go:1.1,1.2 1 1",
		coverageHeader + "example.com/a.go:1.1,1.2 1 X\n",
		coverageHeader + "example.com/a.go:1.1,1.2 X 1\n",
		coverageHeader + "example.com/a.go:1.1,1.2 1 1 extra\n",
		coverageHeader + strings.Repeat("x", maxCoverageLine) + "\n",
	}
	for _, input := range cases {
		var output bytes.Buffer
		if _, err := compact(strings.NewReader(input), &output); err == nil {
			t.Fatalf("accepted malformed input of length %d", len(input))
		}
	}
}

type failureReader struct{}

func (failureReader) Read([]byte) (int, error) { return 0, errors.New("injected read failure") }

type failureWriter struct{}

func (failureWriter) Write([]byte) (int, error) { return 0, errors.New("injected write failure") }

func TestCompactPropagatesReadAndWriteFailures(t *testing.T) {
	input := io.MultiReader(strings.NewReader(coverageHeader), failureReader{})
	if _, err := compact(input, io.Discard); err == nil {
		t.Fatal("read failure was lost")
	}
	input = strings.NewReader(coverageHeader + "example.com/a.go:1.1,1.2 1 1\n")
	if _, err := compact(input, failureWriter{}); err == nil {
		t.Fatal("write failure was lost")
	}
}

func TestCompactFileIsAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "coverage.out")
	invalid := []byte(coverageHeader + "example.com/a.go:1.1,1.2 1 1")
	if err := os.WriteFile(path, invalid, 0600); err != nil {
		t.Fatal(err)
	}
	if err := compactFile(path); err == nil {
		t.Fatal("accepted incomplete profile")
	}
	got, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(got, invalid) {
		t.Fatalf("raw input was changed after failure: data=%q error=%v", got, err)
	}
	files, err := filepath.Glob(path + ".compacted.*")
	if err != nil || len(files) != 0 {
		t.Fatalf("temporary files after failure: %v, %v", files, err)
	}
	valid := []byte(coverageHeader +
		"example.com/a.go:1.1,1.2 1 0\n" +
		"example.com/a.go:1.1,1.2 1 1\n")
	if err := os.WriteFile(path, valid, 0600); err != nil {
		t.Fatal(err)
	}
	if err := compactFile(path); err != nil {
		t.Fatal(err)
	}
	got, err = os.ReadFile(path)
	if err != nil || string(got) != coverageHeader+"example.com/a.go:1.1,1.2 1 1\n" {
		t.Fatalf("wrong compacted profile: data=%q error=%v", got, err)
	}
}
