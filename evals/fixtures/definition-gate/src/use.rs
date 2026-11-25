use crate::add;

pub fn quad(x: i32) -> i32 {
    // add(1, 2) in a comment is not a call
    let s = "add(";
    add(add(x, x), add(x, x))
}
