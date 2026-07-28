"""Pure-function units for the reader-fit geometry, no PDF involved; step 3's
internal ordering is asserted directly because a mistake there is silent."""

import pytest

from scribe_crop.crop_shim import (
    ReaderFit,
    apply_floor,
    content_box,
    modal_page_size,
    place_box,
    rotate_quad,
    union_boxes,
)

PAGE = [0.0, 0.0, 400.0, 600.0]
NO_RETAIN = [0.0, 0.0, 0.0, 0.0]
NO_OFFSET = [0.0, 0.0, 0.0, 0.0]


# --------------------------------------------------------------------------
# Step 3: rotation permutation
# --------------------------------------------------------------------------


def test_rotate_quad_matches_mod_box_for_rotation():
    # Each 90-degree turn maps [L,B,R,T] -> [B,R,T,L], as the vendored tool does.
    quad = [1.0, 2.0, 3.0, 4.0]
    assert rotate_quad(quad, 0) == [1.0, 2.0, 3.0, 4.0]
    assert rotate_quad(quad, 90) == [2.0, 3.0, 4.0, 1.0]  # [B,R,T,L]
    assert rotate_quad(quad, 180) == [3.0, 4.0, 1.0, 2.0]  # [R,T,L,B]
    assert rotate_quad(quad, 270) == [4.0, 1.0, 2.0, 3.0]  # [T,L,B,R]


def test_rotate_quad_matches_vendored_implementation():
    # Guard against drift from the tool we inject into.
    from pdfCropMargins.pymupdf_routines import mod_box_for_rotation

    quad = [1.0, 2.0, 3.0, 4.0]
    for angle in (0, 90, 180, 270):
        assert rotate_quad(quad, angle) == list(mod_box_for_rotation(quad, angle))


def test_rotate_quad_normalizes_out_of_range_angles():
    quad = [1.0, 2.0, 3.0, 4.0]
    assert rotate_quad(quad, 360) == rotate_quad(quad, 0)
    assert rotate_quad(quad, 450) == rotate_quad(quad, 90)


# --------------------------------------------------------------------------
# Step 3: content box, and the ordering within it
# --------------------------------------------------------------------------


def test_content_box_zero_retain_zero_offset_is_the_tight_box():
    tight = [100.0, 100.0, 300.0, 500.0]
    assert content_box(
        tight, PAGE, percent_retain4=NO_RETAIN, absolute4=NO_OFFSET
    ) == tight


def test_content_box_retain_scales_each_edge_own_margin():
    # Margins are L=100, B=100, R=100, T=100 on this page; retaining 50% puts
    # each edge halfway back out toward the page edge.
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight, PAGE, percent_retain4=[50.0] * 4, absolute4=NO_OFFSET
    )
    assert box == pytest.approx([50.0, 50.0, 350.0, 550.0])


def test_content_box_retain_is_relative_to_that_pages_own_margins():
    # A narrower margin retains proportionally less: the retain scales the margin
    # delta, not the box.
    wide = content_box(
        [100.0, 0.0, 400.0, 600.0],
        PAGE,
        percent_retain4=[50.0, 0.0, 0.0, 0.0],
        absolute4=NO_OFFSET,
    )
    narrow = content_box(
        [20.0, 0.0, 400.0, 600.0],
        PAGE,
        percent_retain4=[50.0, 0.0, 0.0, 0.0],
        absolute4=NO_OFFSET,
    )
    assert wide[0] == pytest.approx(50.0)
    assert narrow[0] == pytest.approx(10.0)


def test_content_box_absolute_offset_is_additive_not_scaled():
    # ORDERING: absolute4 applies AFTER the retain scaling; folded in before, it
    # would come out multiplied by (1 - p/100), 6.0 here instead of 12.0.
    tight = [100.0, 100.0, 300.0, 500.0]
    base = content_box(
        tight, PAGE, percent_retain4=[50.0] * 4, absolute4=NO_OFFSET
    )
    offset = content_box(
        tight, PAGE, percent_retain4=[50.0] * 4, absolute4=[0.0, 0.0, 12.0, 0.0]
    )
    assert base[2] - offset[2] == pytest.approx(12.0)


def test_content_box_offset_sign_convention():
    # Positive offsets crop inward on every edge; negative ones widen.
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight, PAGE, percent_retain4=NO_RETAIN, absolute4=[5.0, 6.0, 7.0, 8.0]
    )
    assert box == pytest.approx([105.0, 106.0, 293.0, 492.0])
    widened = content_box(
        tight, PAGE, percent_retain4=NO_RETAIN, absolute4=[-5.0, 0.0, -5.0, 0.0]
    )
    assert widened[0] == pytest.approx(95.0)
    assert widened[2] == pytest.approx(305.0)


def test_content_box_permutes_retain_by_rotation():
    # ORDERING: the quad permutes BEFORE applying; at 180, [L,B,R,T] becomes
    # [R,T,L,B], so a left-only retain lands on the right edge.
    tight = [100.0, 100.0, 300.0, 500.0]
    upright = content_box(
        tight, PAGE, percent_retain4=[50.0, 0.0, 0.0, 0.0], absolute4=NO_OFFSET
    )
    rotated = content_box(
        tight,
        PAGE,
        percent_retain4=[50.0, 0.0, 0.0, 0.0],
        absolute4=NO_OFFSET,
        rotation=180,
    )
    assert upright[0] == pytest.approx(50.0)  # left edge moved
    assert upright[2] == pytest.approx(300.0)
    assert rotated[0] == pytest.approx(100.0)  # left untouched
    assert rotated[2] == pytest.approx(350.0)  # right edge moved instead


def test_content_box_permutes_offset_by_rotation():
    tight = [100.0, 100.0, 300.0, 500.0]
    rotated = content_box(
        tight,
        PAGE,
        percent_retain4=NO_RETAIN,
        absolute4=[12.0, 0.0, 0.0, 0.0],
        rotation=180,
    )
    assert rotated[0] == pytest.approx(100.0)
    assert rotated[2] == pytest.approx(288.0)  # the L offset landed on R


def test_content_box_strip_cut_clamps_the_padded_edge():
    # ORDERING: the cut applies LAST, to the padded box (550 -> 520 here); cut
    # first and the retain would re-expand back over the band (to 535).
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight,
        PAGE,
        percent_retain4=[50.0] * 4,
        absolute4=NO_OFFSET,
        top_cut=520.0,
    )
    assert box[3] == pytest.approx(520.0)


def test_content_box_strip_cut_never_expands():
    # The cut only ever clamps: a cut above the padded top leaves it alone.
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight, PAGE, percent_retain4=NO_RETAIN, absolute4=NO_OFFSET, top_cut=590.0
    )
    assert box[3] == pytest.approx(500.0)


def test_content_box_bottom_cut_clamps_upward():
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight,
        PAGE,
        percent_retain4=[50.0] * 4,
        absolute4=NO_OFFSET,
        bottom_cut=80.0,
    )
    assert box[1] == pytest.approx(80.0)


def test_content_box_full_ordering_end_to_end():
    # All four steps at 180, by hand: permute (L-retain -> R, T-offset -> B),
    # retain (R edge 400 - 100*0.8 = 320), offset (B 100+6), cut (T 500 -> 450).
    tight = [100.0, 100.0, 300.0, 500.0]
    box = content_box(
        tight,
        PAGE,
        percent_retain4=[20.0, 0.0, 0.0, 0.0],
        absolute4=[0.0, 0.0, 0.0, 6.0],
        rotation=180,
        top_cut=450.0,
    )
    assert box == pytest.approx([100.0, 106.0, 320.0, 450.0])


def test_content_box_handles_nonzero_page_origin():
    # Boxes live in the absolute post-precrop frame; origins are not assumed zero.
    page = [50.0, 20.0, 450.0, 620.0]
    tight = [150.0, 120.0, 350.0, 520.0]
    box = content_box(tight, page, percent_retain4=[50.0] * 4, absolute4=NO_OFFSET)
    assert box == pytest.approx([100.0, 70.0, 400.0, 570.0])


# --------------------------------------------------------------------------
# Step 4: cohort selection
# --------------------------------------------------------------------------


def test_modal_page_size_picks_the_majority():
    sizes = [(612.0, 792.0)] * 5 + [(420.0, 595.0)]
    assert modal_page_size(sizes) == (612.0, 792.0)


def test_modal_page_size_absorbs_jitter_within_tolerance():
    sizes = [(612.0, 792.0), (612.5, 791.5), (613.0, 792.0), (420.0, 595.0)]
    assert modal_page_size(sizes) == (612.0, 792.0)


def test_modal_page_size_returns_none_on_a_tie():
    # No unique mode: degrade to page scope rather than pick arbitrarily.
    sizes = [(612.0, 792.0), (612.0, 792.0), (420.0, 595.0), (420.0, 595.0)]
    assert modal_page_size(sizes) is None


def test_modal_page_size_empty():
    assert modal_page_size([]) is None


# --------------------------------------------------------------------------
# Step 5: aggregation
# --------------------------------------------------------------------------


def test_union_boxes_takes_max_extent_per_edge():
    boxes = [
        [100.0, 100.0, 300.0, 500.0],
        [80.0, 120.0, 290.0, 520.0],
        [110.0, 90.0, 310.0, 480.0],
    ]
    assert union_boxes(boxes) == [80.0, 90.0, 310.0, 520.0]


def test_union_of_one_box_is_that_box():
    assert union_boxes([[1.0, 2.0, 3.0, 4.0]]) == [1.0, 2.0, 3.0, 4.0]


def test_capped_votes_cannot_reintroduce_the_band():
    # Votes capped at the document cut cannot pull the shared top back above it,
    # even when a page's own content sits higher.
    cut = 520.0
    votes = []
    for top in (500.0, 560.0, 505.0):  # the middle page has ink in the band zone
        votes.append([100.0, 100.0, 300.0, min(top, cut)])
    assert union_boxes(votes)[3] == pytest.approx(cut)


# --------------------------------------------------------------------------
# Step 6: the floor
# --------------------------------------------------------------------------

SCREEN_W, SCREEN_H = 6.6 * 72, 8.8 * 72  # 475.2 x 633.6 pt
MAX_SCALE = 1.15


def test_floor_does_not_bind_on_a_letter_content_box():
    box = [0.0, 0.0, 468.0, 666.0]
    assert apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE) == box


def test_floor_grows_only_the_binding_dimension():
    # 320x500: 633.6/500 = 1.27 binds; 475.2/320 = 1.48 does not.
    box = [0.0, 0.0, 320.0, 500.0]
    out = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    assert out[2] - out[0] == pytest.approx(320.0)  # width untouched
    assert out[3] - out[1] == pytest.approx(SCREEN_H / MAX_SCALE)


def test_floor_growth_is_symmetric_about_the_centre():
    box = [100.0, 200.0, 420.0, 700.0]
    out = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    assert (out[1] + out[3]) / 2 == pytest.approx((box[1] + box[3]) / 2)
    assert (out[0] + out[2]) / 2 == pytest.approx((box[0] + box[2]) / 2)


def test_floor_realizes_exactly_the_cap():
    box = [0.0, 0.0, 320.0, 500.0]
    out = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    width, height = out[2] - out[0], out[3] - out[1]
    assert min(SCREEN_W / width, SCREEN_H / height) == pytest.approx(MAX_SCALE)


def test_floor_grows_width_when_width_binds():
    # A wide-but-short box: the width ratio is the smaller one.
    box = [0.0, 0.0, 300.0, 200.0]
    out = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    assert out[2] - out[0] == pytest.approx(SCREEN_W / MAX_SCALE)
    assert out[3] - out[1] == pytest.approx(200.0)


def test_floor_tie_breaks_to_the_width():
    # Equal ratios: grow the width (deterministic; either satisfies the cap).
    scale = 2.0
    box = [0.0, 0.0, SCREEN_W / scale, SCREEN_H / scale]
    out = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    assert out[2] - out[0] == pytest.approx(SCREEN_W / MAX_SCALE)
    assert out[3] - out[1] == pytest.approx(SCREEN_H / scale)


def test_floor_is_a_no_op_on_a_degenerate_box():
    assert apply_floor([10.0, 10.0, 10.0, 50.0], SCREEN_W, SCREEN_H, MAX_SCALE) == [
        10.0, 10.0, 10.0, 50.0
    ]


def test_floor_is_closed_form_and_idempotent():
    box = [0.0, 0.0, 320.0, 500.0]
    once = apply_floor(box, SCREEN_W, SCREEN_H, MAX_SCALE)
    twice = apply_floor(once, SCREEN_W, SCREEN_H, MAX_SCALE)
    assert twice == pytest.approx(once)


# --------------------------------------------------------------------------
# Step 8: placement (expand, translate, shrink -- in that order)
# --------------------------------------------------------------------------


def test_place_box_conforming_page_is_unchanged():
    shared = [50.0, 50.0, 350.0, 550.0]
    content = [100.0, 100.0, 300.0, 500.0]
    assert place_box(shared, content, PAGE) == shared


def test_place_box_expands_minimally_for_an_outlier():
    shared = [50.0, 50.0, 350.0, 550.0]
    content = [20.0, 100.0, 300.0, 580.0]  # sticks out left and top
    out = place_box(shared, content, PAGE)
    assert out == [20.0, 50.0, 350.0, 580.0]  # only those two edges moved


def test_place_box_translates_rather_than_shrinking():
    # ORDERING: the box overhangs the left edge but fits the page, so it slides
    # right, preserving its size (and so the floor).
    shared = [-30.0, 50.0, 290.0, 550.0]
    content = [0.0, 100.0, 250.0, 500.0]
    out = place_box(shared, content, PAGE)
    assert out[2] - out[0] == pytest.approx(320.0)  # size preserved
    assert out[0] == pytest.approx(0.0)  # translated into the page
    assert out[2] == pytest.approx(320.0)


def test_place_box_translates_from_the_far_edge_too():
    shared = [150.0, 50.0, 430.0, 550.0]  # overhangs the right edge (page R=400)
    content = [200.0, 100.0, 380.0, 500.0]
    out = place_box(shared, content, PAGE)
    assert out[2] - out[0] == pytest.approx(280.0)
    assert out[2] == pytest.approx(400.0)


def test_place_box_shrinks_only_when_the_page_is_smaller():
    # A box wider than the page clamps to the page edge, never claiming
    # synthetic whitespace beyond it.
    small_page = [0.0, 0.0, 200.0, 300.0]
    box = [-50.0, -50.0, 250.0, 350.0]
    content = [10.0, 10.0, 190.0, 290.0]
    out = place_box(box, content, small_page)
    assert out == [0.0, 0.0, 200.0, 300.0]


def test_place_box_never_shrinks_inside_the_content_box():
    # Goal 4: no step may cut into ink the content box kept.
    box = [50.0, 50.0, 350.0, 550.0]
    content = [10.0, 10.0, 390.0, 590.0]
    out = place_box(box, content, PAGE)
    assert out[0] <= content[0] and out[1] <= content[1]
    assert out[2] >= content[2] and out[3] >= content[3]


def test_place_box_expansion_precedes_translation():
    # Expansion happens FIRST, so translation moves the already-expanded box;
    # the other order would leave content outside the box.
    box = [-40.0, 50.0, 260.0, 550.0]
    content = [-10.0, 100.0, 250.0, 500.0]
    out = place_box(box, content, PAGE)
    assert out[0] <= 0.0 + 1e-9
    assert out[2] >= 250.0


# --------------------------------------------------------------------------
# The ReaderFit directive/fingerprint token
# --------------------------------------------------------------------------


def test_token_round_trips():
    fit = ReaderFit(
        scope="document",
        reader=True,
        exclude_first_page=False,
        screen_w_pt=475.2,
        screen_h_pt=633.6,
        max_scale=1.15,
    )
    assert ReaderFit.parse(fit.token()) == fit


def test_token_round_trips_with_the_floor_off():
    fit = ReaderFit(scope="page", reader=False)
    assert ReaderFit.parse(fit.token()) == fit


def test_token_omits_screen_and_scale_when_the_floor_is_off():
    # Bumping fit_max_scale must not re-crop files with fit_reader = false.
    a = ReaderFit(scope="page", reader=False, screen_w_pt=475.2, max_scale=1.15)
    b = ReaderFit(scope="page", reader=False, screen_w_pt=999.0, max_scale=2.0)
    assert a.token() == b.token()


def test_token_includes_scale_when_the_floor_is_on():
    a = ReaderFit(scope="page", reader=True, max_scale=1.15)
    b = ReaderFit(scope="page", reader=True, max_scale=1.30)
    assert a.token() != b.token()


def test_token_includes_screen_dimensions_when_the_floor_is_on():
    a = ReaderFit(scope="page", reader=True, screen_w_pt=475.2, max_scale=1.15)
    b = ReaderFit(scope="page", reader=True, screen_w_pt=500.0, max_scale=1.15)
    assert a.token() != b.token()


def test_token_always_includes_scope():
    # Scope changes geometry even with the floor off.
    a = ReaderFit(scope="page", reader=False)
    b = ReaderFit(scope="document", reader=False)
    assert a.token() != b.token()


def test_token_includes_first_page_only_under_document_scope():
    doc_a = ReaderFit(scope="document", exclude_first_page=True)
    doc_b = ReaderFit(scope="document", exclude_first_page=False)
    assert doc_a.token() != doc_b.token()
    page_a = ReaderFit(scope="page", exclude_first_page=True)
    page_b = ReaderFit(scope="page", exclude_first_page=False)
    assert page_a.token() == page_b.token()


def test_active_is_false_only_when_nothing_would_change():
    assert ReaderFit(scope="page", reader=False).active is False
    assert ReaderFit(scope="page", reader=True).active is True
    assert ReaderFit(scope="document", reader=False).active is True


def test_parse_rejects_an_unknown_scope():
    with pytest.raises(ValueError):
        ReaderFit.parse("scope=chapter;reader=1")
