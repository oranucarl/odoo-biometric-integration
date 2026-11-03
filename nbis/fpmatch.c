/*
 * Small C entry points around NIST NBIS (mindtct + bozorth3) for use from
 * Python via ctypes. Mirrors what libfprint does in fp-image.c and
 * fpi-print.c (LGPL-2.1).
 */

#include <stdlib.h>
#include <string.h>

#include "lfs.h"
#include "bozorth.h"

/*
 * Extract minutiae from an 8-bit greyscale image (dark ridges on light
 * background). Writes up to MAX_BOZORTH_MINUTIAE (x, y, theta) triples,
 * sorted the way bozorth3 expects, and returns how many were written.
 * Returns a negative value on failure.
 */
int
fp_extract (const unsigned char *pixels, int width, int height, double ppmm,
            int *xs, int *ys, int *thetas)
{
  MINUTIAE *minutiae = NULL;
  int *direction_map = NULL, *low_contrast_map = NULL, *low_flow_map = NULL;
  int *high_curve_map = NULL, *quality_map = NULL;
  unsigned char *binarized = NULL;
  int map_w, map_h, bw, bh, bd;
  struct minutiae_struct c[MAX_BOZORTH_MINUTIAE];
  unsigned char *image;
  int r, i, nmin;

  image = malloc ((size_t) width * height);
  if (!image)
    return -1;
  memcpy (image, pixels, (size_t) width * height);

  r = get_minutiae (&minutiae, &quality_map, &direction_map,
                    &low_contrast_map, &low_flow_map, &high_curve_map,
                    &map_w, &map_h, &binarized, &bw, &bh, &bd,
                    image, width, height, 8, ppmm, &g_lfsparms_V2);

  free (image);
  g_free (direction_map);
  g_free (low_contrast_map);
  g_free (low_flow_map);
  g_free (high_curve_map);
  g_free (quality_map);
  g_free (binarized);

  if (r)
    {
      if (minutiae)
        free_minutiae (minutiae);
      return r > 0 ? -r : r;
    }

  nmin = minutiae->num < MAX_BOZORTH_MINUTIAE ? minutiae->num : MAX_BOZORTH_MINUTIAE;
  for (i = 0; i < nmin; i++)
    {
      MINUTIA *m = minutiae->list[i];

      lfs2nist_minutia_XYT (&c[i].col[0], &c[i].col[1], &c[i].col[2],
                            m, width, height);
      c[i].col[3] = sround (m->reliability * 100.0);
      if (c[i].col[2] > 180)
        c[i].col[2] -= 360;
    }
  free_minutiae (minutiae);

  qsort (c, (size_t) nmin, sizeof (struct minutiae_struct), sort_x_y);

  for (i = 0; i < nmin; i++)
    {
      xs[i] = c[i].col[0];
      ys[i] = c[i].col[1];
      thetas[i] = c[i].col[2];
    }
  return nmin;
}

static void
fill_xyt (struct xyt_struct *s, int n, const int *xs, const int *ys, const int *ts)
{
  if (n > MAX_BOZORTH_MINUTIAE)
    n = MAX_BOZORTH_MINUTIAE;
  memset (s, 0, sizeof (*s));
  s->nrows = n;
  memcpy (s->xcol, xs, n * sizeof (int));
  memcpy (s->ycol, ys, n * sizeof (int));
  memcpy (s->thetacol, ts, n * sizeof (int));
}

/* bozorth3 similarity score between a probe and a gallery print. */
int
fp_match (int n1, const int *x1, const int *y1, const int *t1,
          int n2, const int *x2, const int *y2, const int *t2)
{
  struct xyt_struct *probe = malloc (sizeof (struct xyt_struct));
  struct xyt_struct *gallery = malloc (sizeof (struct xyt_struct));
  int probe_len, score;

  if (!probe || !gallery)
    {
      free (probe);
      free (gallery);
      return -1;
    }
  fill_xyt (probe, n1, x1, y1, t1);
  fill_xyt (gallery, n2, x2, y2, t2);

  probe_len = bozorth_probe_init (probe);
  score = bozorth_to_gallery (probe_len, probe, gallery);

  free (probe);
  free (gallery);
  return score;
}
