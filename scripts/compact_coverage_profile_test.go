package main

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const coverageHeader = "mode: set\n"

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
