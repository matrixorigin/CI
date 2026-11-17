package app

import (
	"os"
	"strconv"
	"testing"
)

func TestActuallyRuns(t *testing.T) {
	want, err := strconv.Atoi(os.Getenv("EXPECTED_VALUE"))
	if err != nil || Value() != want {
		t.Fatalf("computed %d, expected %d (parse error %v)", Value(), want, err)
	}
	f, err := os.OpenFile(os.Getenv("TEST_MARKER"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil { t.Fatal(err) }
	defer f.Close()
	if _, err := f.WriteString("executed\n"); err != nil { t.Fatal(err) }
}
