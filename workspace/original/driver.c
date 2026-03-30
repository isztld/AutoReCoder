/*
 * driver.c — AutoReCoder oracle driver for miniz
 *
 * Reads raw bytes from stdin, compresses them with miniz, then decompresses
 * back, and writes the decompressed bytes to stdout.
 *
 * This gives a round-trip oracle: for any input, the output should equal
 * the input exactly. The Rust translation must produce identical output.
 *
 * Exit codes:
 *   0 — success
 *   1 — compress error
 *   2 — decompress error
 *   3 — round-trip mismatch (should never happen with correct miniz)
 *   4 — I/O error
 */

#include "miniz.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_INPUT (4 * 1024 * 1024)  /* 4 MB max input */

int main(void) {
    /* Read all of stdin */
    unsigned char *input = (unsigned char *)malloc(MAX_INPUT);
    if (!input) {
        fprintf(stderr, "out of memory\n");
        return 4;
    }

    size_t input_len = 0;
    int c;
    while ((c = fgetc(stdin)) != EOF && input_len < MAX_INPUT) {
        input[input_len++] = (unsigned char)c;
    }

    /* Allocate compression buffer (worst case: slightly larger than input) */
    mz_ulong comp_bound = mz_compressBound((mz_ulong)input_len);
    unsigned char *compressed = (unsigned char *)malloc(comp_bound);
    if (!compressed) {
        free(input);
        fprintf(stderr, "out of memory\n");
        return 4;
    }

    /* Compress */
    mz_ulong comp_len = comp_bound;
    int status = mz_compress(compressed, &comp_len, input, (mz_ulong)input_len);
    if (status != MZ_OK) {
        fprintf(stderr, "mz_compress failed: %d\n", status);
        free(input);
        free(compressed);
        return 1;
    }

    /* Decompress back */
    mz_ulong decomp_len = (mz_ulong)(input_len + 1);
    unsigned char *decompressed = (unsigned char *)malloc(decomp_len > 0 ? decomp_len : 1);
    if (!decompressed) {
        free(input);
        free(compressed);
        fprintf(stderr, "out of memory\n");
        return 4;
    }

    status = mz_uncompress(decompressed, &decomp_len, compressed, comp_len);
    if (status != MZ_OK) {
        fprintf(stderr, "mz_uncompress failed: %d\n", status);
        free(input);
        free(compressed);
        free(decompressed);
        return 2;
    }

    /* Verify round-trip */
    if (decomp_len != (mz_ulong)input_len ||
        memcmp(decompressed, input, input_len) != 0) {
        fprintf(stderr, "round-trip mismatch!\n");
        free(input);
        free(compressed);
        free(decompressed);
        return 3;
    }

    /* Write decompressed bytes to stdout */
    fwrite(decompressed, 1, decomp_len, stdout);

    free(input);
    free(compressed);
    free(decompressed);
    return 0;
}
