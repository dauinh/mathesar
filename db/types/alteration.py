from sqlalchemy import text, DDL, MetaData, Table
from db.types import base, email

BOOLEAN = "boolean"
EMAIL = "email"
INTERVAL = "interval"
NUMERIC = "numeric"
STRING = "string"
TEXT = "text"
VARCHAR = "varchar"


def get_supported_alter_column_types(engine):
    dialect_types = engine.dialect.ischema_names
    type_map = {
        # Default Postgres types
        BOOLEAN: dialect_types.get("boolean"),
        INTERVAL: dialect_types.get("interval"),
        NUMERIC: dialect_types.get("numeric"),
        STRING: dialect_types.get("name"),
        # Custom Mathesar types
        EMAIL: dialect_types.get(email.QUALIFIED_EMAIL)
    }
    return {k: v for k, v in type_map.items() if v is not None}


def alter_column_type(
        schema, table_name, column_name, target_type_str, engine
):
    _preparer = engine.dialect.identifier_preparer
    supported_types = get_supported_alter_column_types(engine)
    target_type = supported_types.get(target_type_str.lower())

    with engine.begin() as conn:
        metadata = MetaData(bind=engine, schema=schema)
        table = Table(
            table_name, metadata, schema=schema, autoload_with=engine
        )
        column = table.columns[column_name]
        prepared_table_name = _preparer.format_table(table)
        prepared_column_name = _preparer.format_column(column)
        prepared_type_name = target_type().compile(dialect=engine.dialect)
        cast_function_name = get_cast_function_name(prepared_type_name)
        alter_stmt = f"""
        ALTER TABLE {prepared_table_name}
          ALTER COLUMN {prepared_column_name}
          TYPE {prepared_type_name}
          USING {cast_function_name}({prepared_column_name});
        """
        conn.execute(DDL(alter_stmt))


def install_all_casts(engine):
    create_boolean_casts(engine)
    create_email_casts(engine)
    create_interval_casts(engine)
    create_numeric_casts(engine)
    create_varchar_casts(engine)


def create_boolean_casts(engine):
    """
    Using the `create_cast_functions` python function, we create a
    PL/pgSQL function to cast types corresponding to each of:

    boolean -> boolean:  Identity. No remarks
    text -> boolean:     We only cast 't', 'f', 'true', or 'false' all
                         others raise a custom exception.
    numeric -> boolean:  We only cast 1 -> true, 0 -> false (this is not
                         default behavior for PostgreSQL). Others raise a
                         custom exception.
    """
    not_bool_exception_str = f"RAISE EXCEPTION '% is not a {BOOLEAN}', $1;"
    type_body_map = {
        BOOLEAN: """
        BEGIN
          RETURN $1;
        END;
        """,
        TEXT: f"""
        DECLARE
        istrue {BOOLEAN};
        BEGIN
          SELECT lower($1)='t' OR lower($1)='true' OR $1='1' INTO istrue;
          IF istrue OR lower($1)='f' OR lower($1)='false' OR $1='0' THEN
            RETURN istrue;
          END IF;
          {not_bool_exception_str}
        END;
        """,
        NUMERIC: f"""
        BEGIN
          IF $1<>0 AND $1<>1 THEN
            {not_bool_exception_str}
          END IF;
          RETURN $1<>0;
        END;
        """,
    }
    create_cast_functions(BOOLEAN, type_body_map, engine)


def create_numeric_casts(engine):
    """
    Using the `create_cast_functions` python function, we create a
    PL/pgSQL function to cast types corresponding to each of:

    numeric -> numeric:  Identity. No remarks
    boolean -> numeric:  We cast TRUE -> 1, FALSE -> 0
    text -> numeric:     We use the default PostgreSQL behavior.
    """
    type_body_map = {
        NUMERIC: """
        BEGIN
          RETURN $1;
        END;
        """,
        BOOLEAN: f"""
        BEGIN
          IF $1 THEN
            RETURN 1::{NUMERIC};
          END IF;
          RETURN 0;
        END;
        """,
        TEXT: f"""
        BEGIN
          RETURN $1::{NUMERIC};
        END;
        """,
    }
    create_cast_functions(NUMERIC, type_body_map, engine)


def create_email_casts(engine):
    """
    Using the `create_cast_functions` python function, we create a
    PL/pgSQL function to cast types corresponding to each of:

    email -> email:  Identity. No remarks
    text -> email:   We use the default PostgreSQL behavior (this will
                     just check that the TEXT object satisfies the email
                     DOMAIN).
    """
    type_body_map = {
        email.QUALIFIED_EMAIL: """
        BEGIN
          RETURN $1;
        END;
        """,
        TEXT: f"""
        BEGIN
          RETURN $1::{email.QUALIFIED_EMAIL};
        END;
        """,
    }
    create_cast_functions(email.QUALIFIED_EMAIL, type_body_map, engine)


def create_interval_casts(engine):
    """
    Using the `create_cast_functions` python function, we create a
    PL/pgSQL function to cast types corresponding to each of:

    interval -> interval:  Identity. No remarks
    text -> interval:      We first check that the text *cannot* be cast
                           to a numeric, and then try to cast the text to an
                           interval.
    """
    type_body_map = {
        INTERVAL: """
        BEGIN
          RETURN $1;
        END;
        """,
        # We need to check that a string isn't a valid number before
        # casting to intervals (since a number is more likely)
        TEXT: f"""
        BEGIN
          PERFORM $1::{NUMERIC};
          RAISE EXCEPTION '% is a {NUMERIC}', $1;
          EXCEPTION
            WHEN sqlstate '22P02' THEN
              RETURN $1::{INTERVAL};
        END;
        """,
    }
    create_cast_functions(INTERVAL, type_body_map, engine)


def create_varchar_casts(engine):
    """
    Using the `create_cast_functions` python function, we create a
    PL/pgSQL function to cast types corresponding to each of:

    varchar -> varchar:   Identity. No remarks
    boolean -> varchar:   We use the default PostgreSQL cast behavior.
    email -> varchar:     We use the default PostgreSQL cast behavior.
    interval -> varchar:  We use the default PostgreSQL cast behavior.
    numeric -> varchar:   We use the default PostgreSQL cast behavior.
    text -> varchar:      We use the default PostgreSQL cast behavior.
    """
    type_body_map = {
        VARCHAR: """
        BEGIN
          RETURN $1;
        END;
        """,
        BOOLEAN: f"""
        BEGIN
          RETURN $1::{VARCHAR};
        END;
        """,
        email.QUALIFIED_EMAIL: f"""
        BEGIN
          RETURN $1::{VARCHAR};
        END;
        """,
        INTERVAL: f"""
        BEGIN
          RETURN $1::{VARCHAR};
        END;
        """,
        NUMERIC: f"""
        BEGIN
          RETURN $1::{VARCHAR};
        END;
        """,
        TEXT: f"""
        BEGIN
          RETURN $1::{VARCHAR};
        END;
        """,
    }
    create_cast_functions(VARCHAR, type_body_map, engine)


def create_cast_functions(target_type, type_body_map, engine):
    """
    This python function writes a number of PL/pgSQL functions that cast
    between types supported by Mathesar, and installs them on the DB
    using the given engine.  Each generated PL/pgSQL function has the
    name `cast_to_<target_type>`.  We utilize the function overloading of
    PL/pgSQL to use the correct function body corresponding to a given
    input (source) type.

    Args:
        target_type:   string corresponding to the target type of the
                       cast function.
        type_body_map: dictionary that gives a map between source types
                       and the body of a PL/pgSQL function to cast a
                       given source type to the target type.
        engine:        an SQLAlchemy engine.
    """
    for type_, body in type_body_map.items():
        query = assemble_function_creation_sql(type_, target_type, body)
        with engine.begin() as conn:
            conn.execute(text(query))


def assemble_function_creation_sql(argument_type, target_type, function_body):
    function_name = get_cast_function_name(target_type)
    return f"""
    CREATE OR REPLACE FUNCTION {function_name}({argument_type})
    RETURNS {target_type}
    AS $$
    {function_body}
    $$ LANGUAGE plpgsql;
    """


def get_cast_function_name(target_type):
    unqualified_type_name = target_type.split('.')[-1].lower()
    bare_function_name = f"cast_to_{unqualified_type_name}"
    return f"{base.get_qualified_name(bare_function_name)}"