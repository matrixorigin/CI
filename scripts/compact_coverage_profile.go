// Command compact_coverage_profile merges duplicate mode:set coverage records.
// Go's all-package -coverpkg profile contains one copy of each instrumented
// block per tested package. Keep only each block's logical OR before upload.
package main

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"time"
)

const maxCoverageLine = 1 << 20

var excludedPaths = [][]byte{
	[]byte("pkg/pb"),
	[]byte("pkg/sql/parsers/goyacc"),
	[]byte("yaccpar"),
}

// compact retains one key per distinct (source range, statement count), not
// one key per input row. The common duplicate lookup does not retain a copy of
// the reader's buffer.
func compact(in io.Reader, out io.Writer) (int, error) {
	reader := bufio.NewReaderSize(in, maxCoverageLine)
	header, err := reader.ReadSlice('\n')
	if err != nil || !bytes.Equal(header, []byte("mode: set\n")) {
		return 0, fmt.Errorf("invalid mode:set coverage header: %w", errOrInvalid(err))
	}

	hits := make(map[string]bool, 65536)
	lineNumber := 1
	for {
		line, readErr := reader.ReadSlice('\n')
		if errors.Is(readErr, io.EOF) && len(line) == 0 {
			break
		}
		if readErr != nil {
			return 0, fmt.Errorf("coverage line %d is incomplete or exceeds %d bytes: %w", lineNumber+1, maxCoverageLine, readErr)
		}
		lineNumber++
		line = line[:len(line)-1]
		firstSpace := bytes.IndexByte(line, ' ')
		if firstSpace <= 0 {
			return 0, fmt.Errorf("malformed coverage line %d", lineNumber)
		}
		secondRel := bytes.IndexByte(line[firstSpace+1:], ' ')
		if secondRel <= 0 {
			return 0, fmt.Errorf("malformed coverage line %d", lineNumber)
		}
		secondSpace := firstSpace + 1 + secondRel
		if !decimal(line[firstSpace+1 : secondSpace]) {
			return 0, fmt.Errorf("invalid statement count on coverage line %d", lineNumber)
		}
		count, valid := positiveDecimal(line[secondSpace+1:])
		if !valid {
			return 0, fmt.Errorf("invalid hit count on coverage line %d", lineNumber)
		}

		path := line[:firstSpace]
		excluded := false
		for _, fragment := range excludedPaths {
			if bytes.Contains(path, fragment) {
				excluded = true
				break
			}
		}
		if excluded {
			continue
		}
		keyBytes := line[:secondSpace]
		if previous, exists := hits[string(keyBytes)]; exists {
			if count && !previous {
				hits[string(keyBytes)] = true
			}
			continue
		}
		hits[string(keyBytes)] = count
	}
	if len(hits) == 0 {
		return 0, errors.New("empty compacted coverage profile")
	}

	keys := make([]string, 0, len(hits))
	for key := range hits {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	writer := bufio.NewWriterSize(out, 1<<20)
	if _, err := writer.WriteString("mode: set\n"); err != nil {
		return 0, err
	}
	for _, key := range keys {
		if _, err := writer.WriteString(key); err != nil {
			return 0, err
		}
		if hits[key] {
			if _, err := writer.WriteString(" 1\n"); err != nil {
				return 0, err
			}
		} else {
			if _, err := writer.WriteString(" 0\n"); err != nil {
				return 0, err
			}
		}
	}
	if err := writer.Flush(); err != nil {
		return 0, err
	}
	return len(hits), nil
}

func decimal(value []byte) bool {
	if len(value) == 0 {
		return false
	}
	for _, digit := range value {
		if digit < '0' || digit > '9' {
			return false
		}
	}
	return true
}

func positiveDecimal(value []byte) (bool, bool) {
	if !decimal(value) {
		return false, false
	}
	for _, digit := range value {
		if digit != '0' {
			return true, true
		}
	}
	return false, true
}

func errOrInvalid(err error) error {
	if err != nil {
		return err
	}
	return errors.New("unexpected header")
}

func compactFile(path string) error {
	started := time.Now()
	input, err := os.Open(path)
	if err != nil {
		return err
	}
	defer func() {
		if input != nil {
			_ = input.Close()
		}
	}()
	inputInfo, err := input.Stat()
	if err != nil {
		return err
	}
	if !inputInfo.Mode().IsRegular() {
		return fmt.Errorf("coverage input is not a regular file: %s", path)
	}

	temporary, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".compacted.*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	defer func() {
		if temporary != nil {
			_ = temporary.Close()
		}
	}()
	blocks, err := compact(input, temporary)
	if err != nil {
		return err
	}
	if err := input.Close(); err != nil {
		return err
	}
	input = nil
	if err := temporary.Close(); err != nil {
		return err
	}
	temporary = nil
	outputInfo, err := os.Stat(temporaryPath)
	if err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return err
	}
	fmt.Printf("UT coverage profile compacted from %d to %d bytes; blocks=%d elapsed_seconds=%.1f\n",
		inputInfo.Size(), outputInfo.Size(), blocks, time.Since(started).Seconds())
	return nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: compact_coverage_profile.go COVERAGE_PROFILE")
		os.Exit(2)
	}
	if err := compactFile(os.Args[1]); err != nil {
		fmt.Fprintf(os.Stderr, "coverage profile compaction failed: %v\n", err)
		os.Exit(1)
	}
}
