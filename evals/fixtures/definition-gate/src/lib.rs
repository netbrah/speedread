/// Adds two integers.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub fn twice(x: i32) -> i32 {
    add(x, x)
}

pub fn parse_header(input: &str) -> &str {
    input
}

pub trait Greeting {
    fn greet(&self) -> String;
}

pub struct Bot;

impl Greeting for Bot {
    fn greet(&self) -> String {
        String::from("hi")
    }
}
