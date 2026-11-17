package cgopkg

import (
	"os"
	"strconv"
	"testing"
)

func TestNativeValue(t *testing.T) {
	want, err := strconv.Atoi(os.Getenv("EXPECTED_NATIVE"))
	if err != nil || Value() != want { t.Fatalf("native value: got %d want %d", Value(), want) }
}
