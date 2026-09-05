/**
 * `/api/tools` schema → form → dispatch arguments.
 *
 * Every case here is a literal schema fixture rather than a live fetch, so the
 * derivation rules are pinned independently of whatever the Mac happens to
 * advertise today. The coercion cases are the ones that matter most: they are
 * the difference between a blank optional field being OMITTED and being sent
 * as `""`, which the backend would happily store as the value.
 */

import type { CatalogEntry } from "../../lib/catalog";
import {
  buildArgs,
  clampToField,
  fieldsFor,
  initialValues,
  STEPPER_MAX_RANGE,
  type Field,
} from "../../lib/schemaForm";
import type { JsonSchemaProp, ToolSchema } from "../../lib/types";

function schema(properties: Record<string, JsonSchemaProp>, required: string[] = []): ToolSchema {
  return {
    name: "fixture",
    description: "a fixture",
    cancellable: false,
    reports_progress: false,
    input_schema: { type: "object", properties, required },
  };
}

const bare: CatalogEntry = {
  tool: "fixture",
  group: "Auto edit",
  label: "Fixture",
  description: "a fixture entry for the form tests",
};

const withEntry = (patch: Partial<CatalogEntry>): CatalogEntry => ({ ...bare, ...patch });

function fieldNamed(fields: Field[], name: string): Field {
  const f = fields.find((x) => x.name === name);
  if (f === undefined) throw new Error(`no field named ${name}`);
  return f;
}

describe("widget derivation", () => {
  test("a string with an enum becomes a select carrying its options", () => {
    const f = fieldNamed(fieldsFor(schema({ mode: { type: "string", enum: ["a", "b"] } }), bare), "mode");
    expect(f.widget).toBe("select");
    expect(f.options).toEqual(["a", "b"]);
  });

  test("a boolean becomes a switch and keeps its default", () => {
    const f = fieldNamed(fieldsFor(schema({ upper: { type: "boolean", default: true } }), bare), "upper");
    expect(f.widget).toBe("switch");
    expect(f.default).toBe(true);
    expect(initialValues([f])).toEqual({ upper: true });
  });

  test("a bounded number becomes a stepper clamped to its bounds", () => {
    const f = fieldNamed(
      fieldsFor(schema({ factor: { type: "number", minimum: 1, maximum: 4 } }), bare),
      "factor",
    );
    expect(f.widget).toBe("stepper");
    expect(f.min).toBe(1);
    expect(f.max).toBe(4);
    expect(clampToField(f, 0)).toBe(1);
    expect(clampToField(f, 9)).toBe(4);
    expect(clampToField(f, 3)).toBe(3);
  });

  test("a number bounded over too wide a range stays a text field", () => {
    const wide = fieldNamed(
      fieldsFor(schema({ db: { type: "number", minimum: -60, maximum: 0 } }), bare),
      "db",
    );
    expect(0 - -60).toBeGreaterThan(STEPPER_MAX_RANGE);
    expect(wide.widget).toBe("number");
  });

  test("an unbounded number stays a number field", () => {
    const f = fieldNamed(fieldsFor(schema({ pad: { type: "number", minimum: 0 } }), bare), "pad");
    expect(f.widget).toBe("number");
  });

  test("an integer is marked so its value is rounded on the way out", () => {
    const f = fieldNamed(fieldsFor(schema({ n: { type: "integer" } }), bare), "n");
    expect(f.integer).toBe(true);
    expect(buildArgs([f], { n: "3.7" }).args).toEqual({ n: 4 });
  });

  test("an array becomes a list and an object becomes a mapping", () => {
    const fields = fieldsFor(
      schema({ words: { type: "array", items: { type: "string" } }, mapping: { type: "object" } }),
      bare,
    );
    expect(fieldNamed(fields, "words").widget).toBe("list");
    expect(fieldNamed(fields, "mapping").widget).toBe("mapping");
  });

  test("a nullable string is still a text field — nullability is a UI decision", () => {
    const f = fieldNamed(
      fieldsFor(schema({ bg_color: { type: ["string", "null"], default: "#00FF00" } }), bare),
      "bg_color",
    );
    expect(f.widget).toBe("text");
    expect(f.nullable).toBeUndefined();
  });

  test("an unknown type falls back to text rather than throwing", () => {
    const fields = fieldsFor(schema({ odd: { type: "quaternion" } }), bare);
    expect(fieldNamed(fields, "odd").widget).toBe("text");
  });

  test("a property with no type at all falls back to text", () => {
    const fields = fieldsFor(schema({ mystery: {} }), bare);
    expect(fieldNamed(fields, "mystery").widget).toBe("text");
  });

  test("the label is humanised from the argument name unless the catalogue names it", () => {
    const plain = fieldsFor(schema({ target_lang: { type: "string" } }), bare);
    expect(fieldNamed(plain, "target_lang").label).toBe("Target lang");
    const named = fieldsFor(
      schema({ target_lang: { type: "string" } }),
      withEntry({ fields: { target_lang: { label: "Translate to" } } }),
    );
    expect(fieldNamed(named, "target_lang").label).toBe("Translate to");
  });
});

describe("hide, order and handler-only arguments", () => {
  test("hide removes a field the schema advertises", () => {
    const fields = fieldsFor(
      schema({ num_speakers: { type: "integer" }, turns: { type: "array" } }),
      withEntry({ hide: ["turns"] }),
    );
    expect(fields.map((f) => f.name)).toEqual(["num_speakers"]);
  });

  test("a field marked hidden in its own override is removed too", () => {
    const fields = fieldsFor(
      schema({ a: { type: "string" }, b: { type: "string" } }),
      withEntry({ fields: { b: { hidden: true } } }),
    );
    expect(fields.map((f) => f.name)).toEqual(["a"]);
  });

  test("order pulls named fields to the front and leaves the rest in schema order", () => {
    const fields = fieldsFor(
      schema({ a: { type: "string" }, b: { type: "string" }, c: { type: "string" } }),
      withEntry({ order: ["c", "b"] }),
    );
    expect(fields.map((f) => f.name)).toEqual(["c", "b", "a"]);
  });

  test("order naming a field the schema does not have is ignored, not fatal", () => {
    const fields = fieldsFor(schema({ a: { type: "string" } }), withEntry({ order: ["gone", "a"] }));
    expect(fields.map((f) => f.name)).toEqual(["a"]);
  });

  /** auto_reframe reads `subject_track` but does not advertise it. Without
   *  this the phone would have no way to turn tracking off. */
  test("a catalogue field the schema never mentions is still rendered", () => {
    const fields = fieldsFor(
      schema({ ratio: { type: "string", enum: ["9:16"] } }, ["ratio"]),
      withEntry({ fields: { subject_track: { widget: "switch", default: true } } }),
    );
    expect(fields.map((f) => f.name)).toEqual(["ratio", "subject_track"]);
    expect(fieldNamed(fields, "subject_track").required).toBe(false);
  });
});

describe("seeding", () => {
  test("a required field with no default starts empty and is invalid", () => {
    const fields = fieldsFor(schema({ text: { type: "string" } }, ["text"]), bare);
    const values = initialValues(fields);
    expect(values).toEqual({ text: "" });
    expect(buildArgs(fields, values).errors).toEqual({ text: "Required" });
  });

  test("a required select with no default starts on its first option", () => {
    const fields = fieldsFor(
      schema({ name: { type: "string", enum: ["reels", "shorts"] } }, ["name"]),
      bare,
    );
    expect(initialValues(fields)).toEqual({ name: "reels" });
    expect(buildArgs(fields, initialValues(fields)).errors).toEqual({});
  });

  test("an optional select starts blank so the Mac keeps its own default", () => {
    const fields = fieldsFor(schema({ model: { type: "string", enum: ["turbo"] } }), bare);
    expect(initialValues(fields)).toEqual({ model: "" });
    expect(buildArgs(fields, initialValues(fields)).args).toEqual({});
  });

  test("a default that is no longer one of the options is not seeded", () => {
    const fields = fieldsFor(
      schema({ model: { type: "string", enum: ["turbo"], default: "retired-model" } }, ["model"]),
      bare,
    );
    expect(initialValues(fields)).toEqual({ model: "turbo" });
  });

  test("a catalogue default beats the schema's", () => {
    const fields = fieldsFor(
      schema({ track: { type: "string", default: "v1" } }),
      withEntry({ fields: { track: { widget: "select", options: ["v1", "v2"], default: "v2" } } }),
    );
    expect(initialValues(fields)).toEqual({ track: "v2" });
  });

  test("a switch with no default starts off", () => {
    const fields = fieldsFor(schema({ flag: { type: "boolean" } }), bare);
    expect(initialValues(fields)).toEqual({ flag: false });
  });
});

describe("buildArgs", () => {
  test("a blank optional is omitted, never sent as an empty string", () => {
    const fields = fieldsFor(schema({ language: { type: "string" } }), bare);
    expect(buildArgs(fields, { language: "   " }).args).toEqual({});
  });

  test("a blank optional number is omitted, never sent as NaN", () => {
    const fields = fieldsFor(schema({ pad: { type: "number" } }), bare);
    const built = buildArgs(fields, { pad: "" });
    expect(built.args).toEqual({});
    expect(built.errors).toEqual({});
  });

  test("a number that is not a number is an error, not a NaN on the wire", () => {
    const fields = fieldsFor(schema({ pad: { type: "number" } }), bare);
    expect(buildArgs(fields, { pad: "soon" }).errors).toEqual({ pad: "Needs to be a number" });
  });

  test("a value outside the schema's bounds is refused with the bound named", () => {
    const fields = fieldsFor(schema({ h: { type: "integer", minimum: 16, maximum: 270 } }), bare);
    expect(buildArgs(fields, { h: 4 }).errors).toEqual({ h: "Lowest is 16" });
    expect(buildArgs(fields, { h: 900 }).errors).toEqual({ h: "Highest is 270" });
  });

  test("a numeric enum goes back as a number, not the string the chip carried", () => {
    const fields = fieldsFor(schema({ factor: { type: "integer", enum: [2, 4] } }), bare);
    expect(buildArgs(fields, { factor: "4" }).args).toEqual({ factor: 4 });
  });

  test("a list splits on commas and newlines and drops the empties", () => {
    const fields = fieldsFor(schema({ words: { type: "array" } }), bare);
    expect(buildArgs(fields, { words: "um, uh,\n like ,," }).args).toEqual({
      words: ["um", "uh", "like"],
    });
  });

  test("an empty optional list is omitted", () => {
    const fields = fieldsFor(schema({ words: { type: "array" } }), bare);
    expect(buildArgs(fields, { words: "  , ," }).args).toEqual({});
  });

  test("a mapping parses KEY=VALUE lines and names a line it cannot read", () => {
    const fields = fieldsFor(schema({ mapping: { type: "object" } }, ["mapping"]), bare);
    expect(buildArgs(fields, { mapping: "SPEAKER_00=Host\nSPEAKER_01=Guest" }).args).toEqual({
      mapping: { SPEAKER_00: "Host", SPEAKER_01: "Guest" },
    });
    expect(buildArgs(fields, { mapping: "SPEAKER_00 Host" }).errors.mapping).toContain("KEY=VALUE");
  });

  test("an explicit null on a nullable field survives, and is not treated as blank", () => {
    const fields = fieldsFor(
      schema({ lufs: { type: ["number", "null"], default: -16 } }),
      withEntry({ fields: { lufs: { nullable: { label: "Off" } } } }),
    );
    expect(buildArgs(fields, { lufs: null }).args).toEqual({ lufs: null });
  });

  test("a null on a field that is NOT nullable is omitted rather than sent", () => {
    const fields = fieldsFor(schema({ language: { type: "string" } }), bare);
    expect(buildArgs(fields, { language: null }).args).toEqual({});
  });

  test("a switch always sends its boolean, including false", () => {
    const fields = fieldsFor(schema({ upper: { type: "boolean", default: false } }), bare);
    expect(buildArgs(fields, { upper: false }).args).toEqual({ upper: false });
    expect(buildArgs(fields, { upper: true }).args).toEqual({ upper: true });
  });

  test("text is trimmed", () => {
    const fields = fieldsFor(schema({ text: { type: "string" } }, ["text"]), bare);
    expect(buildArgs(fields, { text: "  a hook  " }).args).toEqual({ text: "a hook" });
  });

  test("errors and args are reported together so every bad field is shown at once", () => {
    const fields = fieldsFor(
      schema({ text: { type: "string" }, start: { type: "number" }, end: { type: "number" } }, [
        "text",
        "start",
        "end",
      ]),
      bare,
    );
    const built = buildArgs(fields, { text: "hi", start: "", end: "nope" });
    expect(built.args).toEqual({ text: "hi" });
    expect(Object.keys(built.errors).sort()).toEqual(["end", "start"]);
  });
});
